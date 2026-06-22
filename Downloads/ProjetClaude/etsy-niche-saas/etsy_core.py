"""
NicheScout core v2 — efficace en credits API.

Idee cle: l'endpoint listings/active renvoie deja, par produit (0 appel boutique) :
  is_personalizable, is_customizable, is_supply, item_weight, when_made,
  materials, tags, taxonomy_id, price.
=> On FILTRE les mauvais produits (vetements, perso, digital, stickers, bijoux,
   electronique, gadgets, trop lourd, trop cheap, vintage, supplies) AU NIVEAU
   LISTING, gratuitement. On ne depense un appel /shops/{id} (ventes + age) QUE
   sur les boutiques dont le produit a deja passe tous les filtres.

Resultat : 1 appel listings = 100 produits filtres ; enrichissement cible
=> beaucoup plus de bonnes boutiques pour bien moins de credits.
"""
import urllib.request, urllib.parse, urllib.error, json, time, os, threading
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

# ---- ANNULATION (bouton STOP): registre token -> Event. La recherche verifie ce
# flag dans ses boucles et s'arrete proprement en RENVOYANT les resultats deja trouves. ----
_CANCELS = {}
def make_cancel(token):
    ev = threading.Event(); _CANCELS[str(token)] = ev; return ev
def cancel_search(token):
    ev = _CANCELS.get(str(token))
    if ev: ev.set()
    return ev is not None
def clear_cancel(token):
    _CANCELS.pop(str(token), None)
def _stopped(stop):
    return bool(stop is not None and stop.is_set())

ENRICH_WORKERS = 4      # appels API Etsy en parallele (+ retry 429 dans _get)
AI_WORKERS = 8          # lots IA en parallele (failover gere par cle) — + de debit
DAY_LIMIT_DEFAULT = 5000  # quota Etsy/jour par defaut (reset 00:00 UTC)

# ---- IA optionnelle (OpenRouter). Modele GLM GRATUIT par defaut => 0 credit. ----
# Cles + modele: variables d'env OU fichier local config.local.json (gitignore).
# Plusieurs cles supportees => failover automatique (les modeles :free sont rate-limited).
def _load_ai_config():
    cfg = {"keys": [], "model": "", "anthropic": "", "etsy": "",
           "glm_key": "", "glm_model": "", "glm_base": ""}
    # 1) env
    env_or = os.environ.get("OPENROUTER_API_KEY", "")
    if env_or:
        cfg["keys"] = [k.strip() for k in env_or.split(",") if k.strip()]
    cfg["model"] = os.environ.get("OPENROUTER_MODEL", "")
    cfg["anthropic"] = os.environ.get("ANTHROPIC_API_KEY", "")
    cfg["etsy"] = os.environ.get("ETSY_API_KEY", "")
    cfg["glm_key"] = os.environ.get("GLM_API_KEY", "")
    cfg["glm_model"] = os.environ.get("GLM_MODEL", "")
    cfg["glm_base"] = os.environ.get("GLM_BASE", "")
    # 2) fichier local (n'ecrase pas l'env)
    p = Path(__file__).parent / "config.local.json"
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8-sig"))
            if not cfg["keys"]:
                ks = d.get("openrouter_keys") or ([d["openrouter_key"]] if d.get("openrouter_key") else [])
                cfg["keys"] = [k for k in ks if k]
            cfg["model"] = cfg["model"] or d.get("openrouter_model", "")
            cfg["anthropic"] = cfg["anthropic"] or d.get("anthropic_key", "")
            cfg["etsy"] = cfg["etsy"] or d.get("etsy_api_key", "")
            cfg["glm_key"] = cfg["glm_key"] or d.get("glm_key", "")
            cfg["glm_model"] = cfg["glm_model"] or d.get("glm_model", "")
            cfg["glm_base"] = cfg["glm_base"] or d.get("glm_base", "")
        except Exception:
            pass
    return cfg

_AICFG = _load_ai_config()
OPENROUTER_KEYS = _AICFG["keys"]
ANTHROPIC_KEY = _AICFG["anthropic"]
# GLM direct (z.ai, OpenAI-compatible) — provider PRIORITAIRE si cle fournie.
GLM_KEY = _AICFG["glm_key"]
GLM_MODEL = _AICFG["glm_model"] or "z-ai/glm-5.2-free"
GLM_BASE = (_AICFG["glm_base"] or "https://zenmux.ai/api/v1").rstrip("/")
AI_MODEL = "claude-haiku-4-5-20251001"   # Anthropic direct (fallback)
# Modeles GRATUITS OpenRouter (0 credit). gpt-oss-120b = meilleur dispo + JSON fiable.
# Chaine de secours si rate-limit (429) sur les modeles :free.
OPENROUTER_MODEL = _AICFG["model"] or "openai/gpt-oss-120b:free"
OPENROUTER_FALLBACKS = ["openai/gpt-oss-20b:free", "meta-llama/llama-3.3-70b-instruct:free",
                        "qwen/qwen3-next-80b-a3b-instruct:free", "nvidia/nemotron-3-super-120b-a12b:free"]

def ai_available():
    return bool(GLM_KEY or OPENROUTER_KEYS or ANTHROPIC_KEY)

def ai_model_name():
    if GLM_KEY: return GLM_MODEL
    if OPENROUTER_KEYS: return OPENROUTER_MODEL
    if ANTHROPIC_KEY: return AI_MODEL
    return ""

def _glm_call(prompt, max_tokens):
    """Appel GLM via ZenMux (OpenAI-compatible). temperature=0 => deterministe.
    GLM 5.2 = reasoning model: les tokens de raisonnement sont decomptes de max_tokens
    AVANT le contenu => on ajoute une marge (+4000) sinon le JSON ressort tronque/vide."""
    budget = max_tokens + 4000
    body = json.dumps({"model": GLM_MODEL, "max_tokens": budget, "temperature": 0,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(GLM_BASE + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + GLM_KEY, "content-type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=120))
    return r["choices"][0]["message"]["content"]

def _openrouter_call(prompt, max_tokens, model, key):
    # temperature=0 => deterministe/precis (pas de creativite). top_p=1.
    body = json.dumps({"model": model, "max_tokens": max_tokens, "temperature": 0, "top_p": 1,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body,
        headers={"Authorization": "Bearer " + key, "content-type": "application/json",
                 "HTTP-Referer": "https://localhost", "X-Title": "CraftPilot"})
    r = json.load(urllib.request.urlopen(req, timeout=90))
    return r["choices"][0]["message"]["content"]

def _ai_call(prompt, max_tokens=2000):
    """1 prompt -> texte. OpenRouter (GLM gratuit) avec failover entre cles + modele
    de secours. Fallback Anthropic direct si pas de cle OpenRouter. '' si echec."""
    if GLM_KEY:                            # GLM prioritaire (le + performant)
        try:
            return _glm_call(prompt, max_tokens)
        except Exception:
            pass                            # echec GLM => bascule sur les fallbacks
    if OPENROUTER_KEYS:
        for model in [OPENROUTER_MODEL] + OPENROUTER_FALLBACKS:
            for key in OPENROUTER_KEYS:
                try:
                    return _openrouter_call(prompt, max_tokens, model, key)
                except Exception:
                    continue
        return ""
    if ANTHROPIC_KEY:
        body = json.dumps({"model": AI_MODEL, "max_tokens": max_tokens, "temperature": 0,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        try:
            r = json.load(urllib.request.urlopen(req, timeout=40))
            return r["content"][0]["text"]
        except Exception:
            return ""
    return ""

# ---- Taxonomie de niches FIXE (large) -----------------------------------------
# Probleme avant: l'IA inventait un nom hyper-specifique par boutique ("Couvercles
# boites tissu", "Figurines laiton vintage") => 1 boutique par niche, impossible
# d'avoir 5/niche, et confusion niche<->produit. Solution: l'IA DOIT choisir une
# niche dans cette liste fermee => les boutiques se regroupent vraiment.
NICHE_TAXONOMY = [
    "Decoration murale",        # tapisseries, macrame, suncatchers, affiches, miroirs, cadres
    "Coussins & linge de maison",
    "Tapis & paillassons",
    "Vases & ceramique deco",
    "Bougies & senteurs",
    "Paniers & rangement tresse",
    "Sacs & pochettes",
    "Accessoires cheveux",
    "Plateaux & vide-poches",
    "Deco de fete & cake toppers",
    "Bain & savon",
    "Peluches & crochet deco",
    "Objets deco en bois",
    "Papeterie & cartes",
    "Accessoires pour animaux",
    "Art de la table & cuisine",
    "Jouets & jeux en bois",
    "Deco de jardin (non vivant)",
    "Autre deco maison",
]

def _ai_refine_chunk(chunk, query=""):
    """1 appel LLM pour un lot de boutiques. Agent autonome: classe chaque boutique
    en examinant ses titres UN PAR UN puis decide la niche majoritaire. Si `query`
    fournie, l'IA juge AUSSI la pertinence semantique vs la recherche (ex: 'support'
    = un support/socle, PAS 'emotional support')."""
    items = [{"id": s["id"], "titres": (s.get("titles") or [s.get("sample", "")])[:60]}
             for s in chunk]
    rel_rule = ""
    rel_field = ""
    if query:
        rel_rule = (
            "\nPERTINENCE RECHERCHE (CRITIQUE): l'utilisateur cherche \"" + query + "\". "
            "Comprends cette recherche SEMANTIQUEMENT comme une CATEGORIE/THEME (le vrai "
            "univers voulu), pas comme une chaine de caracteres. Ex: 'Kitchen & dining decor' "
            "= univers cuisine/salle a manger (vaisselle, ustensiles, deco cuisine, textile "
            "de table, rangement cuisine...). 'support' = un socle physique, PAS 'emotional "
            "support'.\n"
            "METHODE match: parcours TOUS les titres de la boutique, compte combien "
            "appartiennent VRAIMENT a la categorie cherchee. match=true SEULEMENT si la "
            "MAJORITE (>50%) des titres relevent de cette categorie/theme. Si la boutique "
            "vend surtout autre chose (meme si 1-2 titres collent), match=false. Sois STRICT: "
            "mieux vaut rejeter une boutique limite que polluer les resultats avec du hors-sujet.\n"
        )
        rel_field = "\"match\":true,"
    prompt = (
        "Tu es un AGENT autonome d'analyse de niches Etsy pour un dropshipper. Tu recois "
        "plusieurs boutiques, chacune avec la LISTE complete de ses titres produits.\n\n"
        "Prends ton TEMPS. Sois PRECIS et RIGOUREUX. N'invente rien: base-toi uniquement "
        "sur les titres fournis.\n\n"
        "METHODE OBLIGATOIRE pour CHAQUE boutique:\n"
        "1. Lis ABSOLUMENT TOUS les titres, UN PAR UN, sans en sauter. Pour chaque titre, "
        "identifie le type de produit reel (ex: 'macrame wall hanging' -> tenture murale ; "
        "'soy candle' -> bougie parfumee ; 'faux potted plant' -> plante artificielle).\n"
        "2. Compte precisement combien de titres tombent dans chaque type de produit.\n"
        "3. La niche de la boutique = le type de produit STRICTEMENT MAJORITAIRE (le plus de "
        "titres). Ignore les titres isoles/exceptions. Une boutique a UNE seule niche dominante.\n"
        "4. Verifie ta conclusion: relis les titres et confirme que ta niche couvre bien la "
        "majorite avant de repondre. En cas de doute, choisis le type qui couvre le plus de titres.\n"
        + rel_rule +
        "\nRenvoie par boutique:\n"
        "- accept (bool): true SEULEMENT si la majorite des produits sont PHYSIQUES, NON "
        "personnalises, non digitaux, sans vetement/bijou/sticker/porte-cles/electronique/"
        "gadget, pas trop lourds, sans croyance/occulte. Sinon false.\n"
        + ("- match (bool): true si la boutique vend bien le produit cherche (voir PERTINENCE). Sinon false.\n" if query else "") +
        "- niche (str): TU DEDUIS LIBREMENT le nom de la niche a partir des titres (pas de liste "
        "imposee). REGLES STRICTES pour ce nom:\n"
        "   * en francais, 2 a 4 mots, decrivant la CATEGORIE de produit (pas un produit precis).\n"
        "   * assez GENERIQUE pour que d'autres boutiques du meme type tombent dans la MEME niche "
        "(ex BON: 'Fleurs artificielles', 'Bougies parfumees', 'Tentures murales macrame', "
        "'Paniers en osier'). \n"
        "   * ni trop large (PAS 'Decoration maison', 'Artisanat') ni trop precis (PAS "
        "'Bouquet de roses rouges en soie 5 tiges').\n"
        "   * REGROUPE les sous-types proches sous UN meme nom: fleurs + plantes + arbres "
        "artificiels => 'Fleurs & plantes artificielles' (PAS 3 niches separees). Idem "
        "interieur/exterieur = meme niche.\n"
        "   * nomme par le PRODUIT, jamais par un theme/occasion/style.\n"
        "- dropship (float 0..1): proba que ces produits soient INDUSTRIELS revendus, "
        "trouvables A L'IDENTIQUE sur AliExpress/Alibaba. 0.8-1 = generique mass-produit "
        "(led, gadget, deco usine, print-on-demand). 0-0.2 = vrai artisanat unique fait main.\n"
        "- reason (str): 4-8 mots citant le produit dominant observe.\n"
        "Reponds UNIQUEMENT en JSON compact, rien d'autre: "
        "{\"r\":[{\"id\":\"..\",\"accept\":true," + rel_field + "\"niche\":\"..\",\"dropship\":0.1,\"reason\":\"..\"}]}\n"
        + json.dumps(items, ensure_ascii=False)
    )
    out = {}
    txt = _ai_call(prompt, max_tokens=3000)
    if txt:
        try:
            txt = txt[txt.find("{"): txt.rfind("}") + 1]
            for x in json.loads(txt).get("r", []):
                out[x["id"]] = x
        except Exception:
            pass
    return out

def _ai_sig(query, shop):
    """Signature stable d'un verdict IA: depend de la recherche + des titres de la boutique.
    Memes titres + meme query => meme verdict => reutilisable depuis le cache."""
    import hashlib
    titles = (shop.get("titles") or [shop.get("sample", "")])[:40]
    base = (query or "").lower() + "|" + "|".join(sorted(t.lower() for t in titles if t))
    return hashlib.md5(base.encode("utf-8", "replace")).hexdigest()

def _ai_cache_path():
    return CACHE / "ai_verdicts.json"   # CACHE defini plus bas => resolution a l'appel
def _ai_cache_load():
    try:
        return json.loads(_ai_cache_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
def _ai_cache_save(d):
    try:
        # borne la taille (garde les 5000 derniers verdicts) pour ne pas grossir sans fin
        if len(d) > 5000:
            d = dict(list(d.items())[-5000:])
        _ai_cache_path().write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

def ai_refine(shops, batch=8, query=""):
    """Cerveau du logiciel. Decoupe en lots et les traite EN PARALLELE (gros gain
    vitesse, le failover de cles reste gere par lot). Par boutique:
      - accept: vrai produit physique vendable (pas perso/digital/vetement/bijou/gadget/lourd).
      - match: (si query) pertinence semantique vs la recherche.
      - niche: choisie DANS la taxonomie fixe (regroupement reel, pas de nom invente).
      - dropship: 0..1 = proba produits industriels revendus (trouvables identiques sur AliExpress).
    Retourne dict id -> {accept, match, niche, dropship, reason}. {} si pas de cle."""
    if not ai_available() or not shops:
        return {}
    pool = shops[:200]
    # CACHE VERDICTS: l'IA donne le meme verdict pour une boutique tant que ses titres et
    # la recherche n'ont pas change. On reutilise donc les verdicts deja calcules (persistes
    # entre runs) => on n'appelle le LLM QUE sur les boutiques nouvelles/modifiees. Gros gain
    # vitesse + 0 token gaspille a re-juger les memes boutiques.
    vcache = _ai_cache_load()
    out = {}; todo = []
    for s in pool:
        c = vcache.get(_ai_sig(query, s))
        if c is not None:
            out[s["id"]] = c
        else:
            todo.append(s)
    if todo:
        chunks = [todo[i:i + batch] for i in range(0, len(todo), batch)]
        from functools import partial
        work = partial(_ai_refine_chunk, query=query)
        new = {}
        with ThreadPoolExecutor(max_workers=min(AI_WORKERS, len(chunks))) as ex:
            for d in ex.map(work, chunks):
                new.update(d)
        for s in todo:                          # indexe par sig pour les prochains runs
            v = new.get(s["id"])
            if v is not None:
                out[s["id"]] = v
                vcache[_ai_sig(query, s)] = v
        _ai_cache_save(vcache)
    return out

# Niche FR (taxonomie) -> mots-cles ANGLAIS pour la recherche Etsy/scrape. Etsy
# indexe en anglais: taper "Peluches & crochet deco" renvoie 0. On traduit.
NICHE_SEARCH_KW = {
    "Decoration murale": "wall decor",
    "Coussins & linge de maison": "pillow cover",
    "Tapis & paillassons": "tufted rug",
    "Vases & ceramique deco": "ceramic vase",
    "Bougies & senteurs": "candle holder",
    "Paniers & rangement tresse": "woven basket",
    "Sacs & pochettes": "tote bag",
    "Accessoires cheveux": "hair clip",
    "Plateaux & vide-poches": "trinket tray",
    "Deco de fete & cake toppers": "cake topper",
    "Bain & savon": "soap bar",
    "Peluches & crochet deco": "crochet plush",
    "Objets deco en bois": "wood decor",
    "Papeterie & cartes": "greeting card",
    "Accessoires pour animaux": "dog bandana",
    "Art de la table & cuisine": "ceramic mug",
    "Jouets & jeux en bois": "wooden toy",
    "Deco de jardin (non vivant)": "garden decor",
    "Autre deco maison": "home decor",
}

def _strip_accents(s):
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))

# Mots FR -> mots-cles EN pour la recherche Etsy (index anglais). Etsy ne comprend
# PAS le francais: "rangement", "bougie", "tapis"... renvoient n'importe quoi. On
# traduit les mots produit courants. Cle = mot FR sans accent, minuscule.
FR_EN_WORD = {
    "rangement": "storage basket", "panier": "woven basket", "paniers": "woven basket",
    "boite": "storage box", "boites": "storage box", "corbeille": "woven basket",
    "bougie": "candle", "bougies": "candle", "senteur": "scented candle",
    "tapis": "rug", "paillasson": "doormat", "carpette": "rug",
    "coussin": "pillow cover", "coussins": "pillow cover", "housse": "pillow cover",
    "linge": "linen", "couverture": "throw blanket", "plaid": "throw blanket",
    "sac": "tote bag", "sacs": "tote bag", "pochette": "pouch", "trousse": "pouch",
    "vase": "ceramic vase", "ceramique": "ceramic", "poterie": "pottery",
    "miroir": "wall mirror", "cadre": "picture frame", "affiche": "art print",
    "macrame": "macrame wall hanging", "tenture": "wall hanging", "suspension": "wall hanging",
    "plateau": "serving tray", "videpoche": "trinket tray", "coupelle": "trinket dish",
    "savon": "soap bar", "bain": "bath", "diffuseur": "reed diffuser", "encens": "incense holder",
    "peluche": "crochet plush", "peluches": "crochet plush", "crochet": "crochet",
    "bois": "wood decor", "cheveux": "hair clip", "barrette": "hair clip", "chouchou": "scrunchie",
    "papeterie": "stationery", "carte": "greeting card", "cartes": "greeting card",
    "animaux": "dog bandana", "chien": "dog bandana", "chat": "cat collar",
    "cuisine": "ceramic mug", "tasse": "ceramic mug", "mug": "ceramic mug", "assiette": "ceramic plate",
    "jouet": "wooden toy", "jouets": "wooden toy", "jeu": "wooden toy",
    "jardin": "garden decor", "fete": "party decor", "gateau": "cake topper",
    "deco": "home decor", "decoration": "home decor", "murale": "wall decor", "maison": "home decor",
    "bijou": "necklace", "collier": "necklace", "boucle": "earrings", "bracelet": "bracelet",
    # vegetal artificiel (Etsy = anglais): plante/fleur seules -> 'plant'/'flower',
    # 'artificiel*'/'faux'/'fausse' -> 'faux' (mot-cle Etsy le plus courant pour le non-vivant)
    "plante": "plant", "plantes": "plant", "fleur": "flower", "fleurs": "flower",
    "artificiel": "faux", "artificielle": "faux", "artificiels": "faux", "artificielles": "faux",
    "fausse": "faux", "fausses": "faux", "faux": "faux", "fauxplant": "faux plant",
    "succulente": "succulent", "succulentes": "succulent", "bouquet": "bouquet",
    "pampa": "pampas grass", "pampas": "pampas grass", "feuillage": "greenery",
    "eucalyptus": "eucalyptus", "arbre": "tree", "arbres": "tree", "branche": "branch",
}

def resolve_keyword(kw):
    """Traduit un mot-cle FR (nom de niche OU mots produit) en anglais pour Etsy.
    Traduit CHAQUE mot FR connu et GARDE les mots anglais/inconnus (ex: 'support bois'
    -> 'support wood'). Sinon nom de niche complet -> mots-cles EN. Sinon intact.
    Retourne (keyword_effectif, traduit_bool)."""
    raw = (kw or "").strip()
    if not raw:
        return raw, False
    import re as _re
    words = [w for w in _re.findall(r"[a-z]+", _strip_accents(raw).lower()) if len(w) > 1]
    kset = set(w for w in words if len(w) > 2)
    if not kset:
        return raw, False
    # 1) traduction mot-a-mot: traduit les mots FR connus, garde les mots EN/inconnus
    tokens = []; translated = False
    for w in words:
        if w in FR_EN_WORD:
            tokens += FR_EN_WORD[w].split(); translated = True
        elif len(w) > 2:
            tokens.append(w)               # mot deja anglais (ex: 'support')
    if translated:
        seen = list(dict.fromkeys(tokens))  # dedup, garde l'ordre
        return " ".join(seen), True
    # 2) nom de niche complet -> mots-cles EN (seuil 60% des mots de la niche)
    norm = lambda s: set(w for w in _re.findall(r"[a-z]+", _strip_accents(s).lower()) if len(w) > 2)
    best, score = None, 0.0
    for niche, eng in NICHE_SEARCH_KW.items():
        nset = norm(niche)
        if not nset:
            continue
        ov = len(kset & nset) / len(nset)
        if ov > score:
            best, score = eng, ov
    if best and score >= 0.6:
        return best, True
    return raw, False

# Deux niveaux de mots generiques:
# - CORE: mots vraiment vides (home/decor/gift...) - ignores partout.
# - TYPES: types de produit fourre-tout (organizer/holder/box/shelf...). Ils servent a
#   mesurer la DOMINANCE catalogue (une boutique pleine de "pen holder/organizer" EST
#   une boutique bureau) mais PAS comme pre-filtre (sinon une etagere "Wooden Organizer"
#   passerait). Le pre-filtre exige le mot DISTINCTIF (ex "desk").
_REL_GENERIC_CORE = {"decor", "home", "handmade", "gift", "set", "wall", "art", "deco", "custom"}
_REL_GENERIC_TYPES = {"organizer", "organiser", "organize", "holder", "stand", "box", "boxes",
    "storage", "tray", "rack", "shelf", "shelves", "mount", "sign", "case", "container",
    "caddy", "kit", "accessory", "accessories", "office"}
_REL_GENERIC = _REL_GENERIC_CORE | _REL_GENERIC_TYPES   # compat (pre-filtre distinctif)

def keyword_relevance(titles, kw_en):
    """DOMINANCE catalogue: part des titres contenant un mot du mot-cle (types produit
    INCLUS). Une boutique pleine de 'pen holder / desk organizer' score haut; une
    boutique crochet avec 1 'desk' isole score bas. Ignore seulement les mots vides."""
    import re as _re
    toks = [w for w in _re.findall(r"[a-z]+", (kw_en or "").lower()) if len(w) > 2]
    strong = [w for w in toks if w not in _REL_GENERIC_CORE] or toks
    if not strong or not titles:
        return 1.0
    low = [t.lower() for t in titles]
    n = sum(1 for t in low if any(w in t for w in strong))
    return n / len(low)

def _strong_tokens(kw_en):
    """Mots DISTINCTIFS (types produit exclus) pour le pre-filtre precision (ex 'desk')."""
    import re as _re
    toks = [w for w in _re.findall(r"[a-z]+", (kw_en or "").lower()) if len(w) > 2]
    return [w for w in toks if w not in _REL_GENERIC] or toks

def match_sample(titles, kw_en):
    """Renvoie un titre du catalogue qui contient le mot-cle cherche (pour l'affichage
    colonne Produit). A defaut, le 1er titre."""
    if not titles:
        return ""
    strong = _strong_tokens(kw_en)
    if strong:
        for t in titles:
            tl = t.lower()
            if any(w in tl for w in strong):
                return t
    return titles[0]

# Normalisation des noms de niche LIBRES generes par l'IA: deux libelles equivalents
# ("Fleurs artificielles" / "Fausses fleurs & plantes") doivent retomber sur la MEME
# cle de regroupement, sinon chaque boutique fait sa propre niche.
_NICHE_SYN = {  # synonymes -> forme canonique (familles de produits regroupees largement)
    "faux": "artificiel", "fausse": "artificiel", "fausses": "artificiel",
    "fake": "artificiel", "artificielle": "artificiel", "artificielles": "artificiel",
    "artificiels": "artificiel", "synthetique": "artificiel", "soie": "artificiel",
    # famille vegetale -> 'plante' (fleurs, plantes, arbres, feuillages... = meme niche large)
    "fleur": "plante", "fleurs": "plante", "floral": "plante", "florale": "plante",
    "plante": "plante", "plantes": "plante", "plant": "plante", "arbre": "plante",
    "arbres": "plante", "tree": "plante", "feuillage": "plante", "eucalyptus": "plante",
    "succulente": "plante", "succulentes": "plante", "bouquet": "plante", "tige": "plante",
    "tiges": "plante", "pampa": "plante", "pampas": "plante", "branche": "plante",
    # bougies
    "bougie": "bougie", "bougies": "bougie", "candle": "bougie", "bougeoir": "bougie",
    "senteur": "bougie", "parfumee": "bougie",
    # paniers / rangement tresse
    "panier": "panier", "paniers": "panier", "basket": "panier", "osier": "panier",
    "rotin": "panier", "rangement": "panier",
    # textile
    "coussin": "coussin", "coussins": "coussin", "housse": "coussin",
    "tapis": "tapis", "paillasson": "tapis",
    # ceramique
    "vase": "vase", "vases": "vase", "ceramique": "vase", "poterie": "vase",
    # murale
    "tenture": "tenture", "tentures": "tenture", "macrame": "tenture",
    "suncatcher": "tenture", "miroir": "tenture",
    "sac": "sac", "sacs": "sac",
}
_NICHE_FILLER = {"deco", "decoration", "decorative", "maison", "home", "interieur",
                 "exterieur", "exterieure", "exterieurs", "exterieures", "indoor", "outdoor",
                 "art", "style", "moderne", "boho", "pour", "avec", "set", "collection",
                 "grand", "grande", "petit", "petite", "mini", "large", "long", "longue",
                 "soja", "soy", "cire", "wax", "naturel", "naturelle", "fait", "main",
                 "the", "and", "les", "des", "une", "produit", "produits"}

def niche_canon(name):
    """Cle de regroupement stable pour un nom de niche libre. Accents/pluriels/synonymes
    normalises, mots vides retires, tokens tries. '' si vide."""
    import re as _re
    toks = []
    for w in _re.findall(r"[a-z]+", _strip_accents((name or "").lower())):
        if len(w) <= 2 or w in _NICHE_FILLER:
            continue
        # synonyme direct, sinon on singularise PUIS on retente le synonyme
        if w in _NICHE_SYN:
            w = _NICHE_SYN[w]
        else:
            sg = w[:-1] if (len(w) > 4 and w.endswith("s")) else w
            w = _NICHE_SYN.get(sg, sg)
        if w in _NICHE_FILLER:
            continue
        toks.append(w)
    return " ".join(sorted(set(toks)))

def snap_niche(name):
    """Force une niche IA dans la taxonomie fixe (tolerant aux variantes). Retourne
    toujours une valeur de NICHE_TAXONOMY => regroupement garanti."""
    import re as _re
    if not name:
        return "Autre deco maison"
    norm = lambda s: set(w for w in _re.findall(r"[a-z]+", s.lower()) if len(w) > 2)
    want = norm(name)
    if not want:
        return "Autre deco maison"
    best, score = "Autre deco maison", 0
    for n in NICHE_TAXONOMY:
        ov = len(want & norm(n))
        if ov > score:
            best, score = n, ov
    # match exact (recopie) prioritaire
    for n in NICHE_TAXONOMY:
        if name.strip().lower() == n.lower():
            return n
    return best if score else "Autre deco maison"

def ai_enrich_shops(shops, f):
    """Applique le verdict IA aux boutiques. Retourne (shops_filtres, ai_used).
    - accept=false => boutique retiree (vrai tri intelligent).
    - ai_niche => repartition niche fiable (prioritaire sur le clustering mot-cle).
    - ai_dropship/ai_reason stockes pour affichage.
    - si f['ai_dropship_gate'] et f['ali_gate'] : ne garde que les boutiques jugees
      dropship-ables (produits trouvables sur AliExpress), seuil f['dropship_min'] (def 0.5)."""
    if not f.get("use_ai") or not ai_available() or not shops:
        return shops, False
    # phrase BRUTE tapee par l'utilisateur (semantique fidele) sinon mot-cle traduit
    query = (f.get("_query_raw") or f.get("_query") or "").strip()
    verdict = ai_refine(shops, query=query)
    if not verdict:
        return shops, False
    thr = float(f.get("dropship_min", 0.5))
    gate_ds = bool(f.get("ai_dropship_gate"))
    # COUVERTURE: si l'IA a juge la majorite des boutiques, son verdict est fiable => on
    # peut etre STRICT (une boutique sans verdict explicite de match est traitee comme
    # hors-sujet quand un mot-cle est tape). Si l'IA a majoritairement echoue (peu de
    # verdicts), on reste tolerant pour ne pas tout supprimer a tort.
    coverage = len(verdict) / max(len(shops), 1)
    strict = bool(query) and coverage >= 0.5
    kept = []
    # 1er passage: applique verdict + collecte les libelles libres par cle de regroupement
    canon_labels = {}                      # canon -> Counter(libelles bruts)
    from collections import Counter
    for s in shops:
        v = verdict.get(s["id"])
        if v is None:
            # pas de verdict: en mode STRICT (mot-cle + IA fiable) on JETTE (hors-sujet par
            # defaut). Sinon on garde (l'IA n'a pas pu juger, on ne penalise pas).
            if strict:
                continue
            kept.append(s); continue
        if not v.get("accept", True):
            continue                            # IA rejette: hors cible
        if query and v.get("match") is not True:
            continue                            # match doit etre EXPLICITEMENT true (strict)
        raw = (str(v.get("niche") or "")).strip() or "Divers"
        key = niche_canon(raw) or raw.lower()
        canon_labels.setdefault(key, Counter())[raw] += 1
        s["_niche_key"] = key; s["ai_niche_raw"] = raw
        if "dropship" in v:
            try: s["ai_dropship"] = round(float(v["dropship"]), 2)
            except Exception: pass
        if v.get("reason"):
            s["ai_reason"] = str(v["reason"])
        if gate_ds and s.get("ai_dropship") is not None and s["ai_dropship"] < thr:
            continue                            # produit trop unique => pas dropship-able
        kept.append(s)
    # libelle d'affichage par cle = le plus frequent (a egalite, le plus court)
    display = {k: sorted(cnt.items(), key=lambda kv: (-kv[1], len(kv[0])))[0][0]
               for k, cnt in canon_labels.items()}
    for s in kept:
        k = s.get("_niche_key")
        if k:                                  # variantes equivalentes => meme niche affichee
            s["ai_niche"] = display[k]; s["_niche"] = display[k]
    return kept, True

# Cle API Etsy: env ETSY_API_KEY ou config.local.json (jamais en dur / jamais commitee).
API_KEY = _AICFG.get("etsy", "")
BASE = "https://openapi.etsy.com/v3/application"
CACHE = Path(__file__).parent / "cache"; CACHE.mkdir(exist_ok=True)
SHOP_CACHE = CACHE / "shops.json"
CURSOR = CACHE / "cursor.json"   # rotation paging discovery (evite memes niches a chaque run)
QUOTA_F = CACHE / "quota.json"   # dernier quota Etsy connu (persiste entre runs)
NOW = lambda: datetime.now(timezone.utc)
def _today_utc():
    return NOW().strftime("%Y-%m-%d")
def _quota_load():
    try:
        d = json.loads(QUOTA_F.read_text(encoding="utf-8"))
        t = d.get("today")
        try: t = int(t) if t is not None else None
        except Exception: t = None
        return {"today": t, "limit": int(d.get("limit") or DAY_LIMIT_DEFAULT),
                "date": d.get("date")}
    except Exception:
        return {"today": None, "limit": DAY_LIMIT_DEFAULT, "date": None}
_remaining = _quota_load()

def _persist_quota():
    try: QUOTA_F.write_text(json.dumps(_remaining), encoding="utf-8")
    except Exception: pass

def _update_quota(rem, lim):
    """Maj TEMPS REEL depuis les headers Etsy a chaque appel API."""
    if lim is not None:
        try: _remaining["limit"] = int(lim)
        except Exception: pass
    if rem is not None:
        try: _remaining["today"] = int(rem)
        except Exception: pass
        _remaining["date"] = _today_utc()
        _persist_quota()

# ---------------- cache ----------------
def _load():
    return json.loads(SHOP_CACHE.read_text(encoding="utf-8-sig")) if SHOP_CACHE.exists() else {}
def _save(d):
    SHOP_CACHE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

def _name_index(cache):
    """nom_minuscule -> cle. Sert a dedupliquer entre modes (api/scrape) ou
    une meme boutique peut arriver sous une cle numerique (id) ou son nom."""
    idx = {}
    for k, v in cache.items():
        nm = (v.get("name") or "").strip().lower()
        if nm:
            idx[nm] = k
    return idx

def cache_upsert(cache, rec):
    """Ajoute/fusionne rec dans le cache SANS doublon (dedup par nom de boutique,
    tous modes confondus). Garde la version au catalogue le plus riche. Retourne
    la cle utilisee, ou None si rec invalide. N'ecrit PAS le disque (appelant _save)."""
    if not rec or rec.get("sold") is None:
        return None
    nm = (rec.get("name") or "").strip().lower()
    idx = _name_index(cache)
    if nm and nm in idx:
        k = idx[nm]
        old = cache[k]
        # fusion: on garde le catalogue (titles) le plus complet + champs non vides recents
        if len(rec.get("titles") or []) >= len(old.get("titles") or []):
            for kk, vv in rec.items():
                if vv not in (None, "", []):
                    old[kk] = vv
        return k
    key = str(rec.get("id") or rec.get("name"))
    cache[key] = rec
    return key

def _cursor_get(key):
    try: return int(json.loads(CURSOR.read_text(encoding="utf-8")).get(key, 0))
    except Exception: return 0
def _cursor_set(key, val):
    d = {}
    try: d = json.loads(CURSOR.read_text(encoding="utf-8"))
    except Exception: pass
    d[key] = int(val)
    try: CURSOR.write_text(json.dumps(d), encoding="utf-8")
    except Exception: pass

# ---------------- api ----------------
def _get(path, _tries=4):
    """Appel API Etsy avec RETRY sur 429 (rate-limit) et 5xx. Indispensable car on
    enrichit en parallele (rafales) => Etsy renvoie ponctuellement 429. Backoff
    exponentiel (respecte Retry-After si fourni). Quota maj en temps reel."""
    req = urllib.request.Request(BASE + path, headers={"x-api-key": API_KEY})
    for attempt in range(_tries):
        try:
            r = urllib.request.urlopen(req, timeout=25)
            raw = r.read(); headers = r.headers
            break
        except urllib.error.HTTPError as e:       # 401/429/5xx => corps souvent non-JSON
            _update_quota(e.headers.get("x-remaining-today"), e.headers.get("x-limit-per-day"))
            if e.code in (429, 500, 502, 503, 504) and attempt < _tries - 1:
                try: wait = float(e.headers.get("Retry-After") or 0)
                except Exception: wait = 0
                time.sleep(wait or (0.6 * (2 ** attempt)))   # backoff: 0.6,1.2,2.4s
                continue
            raw = e.read()
            try: msg = json.loads(raw.decode("utf-8", "replace")).get("error", "")
            except Exception: msg = raw.decode("utf-8", "replace")[:200]
            raise RuntimeError(f"Etsy API {e.code} {path}: {msg or e.reason}")
        except urllib.error.URLError as e:         # reseau/timeout => retry
            if attempt < _tries - 1:
                time.sleep(0.6 * (2 ** attempt)); continue
            raise RuntimeError(f"Etsy reseau {path}: {e.reason}")
    _update_quota(headers.get("x-remaining-today"), headers.get("x-limit-per-day"))  # temps reel
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        raise RuntimeError(f"Etsy reponse non-JSON {path}: {raw[:200]!r}")

def _maybe_reset_quota():
    """Reset auto: Etsy reinitialise le quota a 00:00 UTC. Si la derniere valeur
    connue date d'un jour anterieur, on remet le compteur a la limite (5000)."""
    today = _today_utc()
    if _remaining.get("date") and _remaining["date"] != today:
        _remaining["today"] = _remaining.get("limit") or DAY_LIMIT_DEFAULT
        _remaining["date"] = today
        _persist_quota()

def quota_remaining(force=False):
    """Quota Etsy restant, TEMPS REEL. Maj a chaque appel API (header x-remaining-today).
    Reset auto a la limite (5000) quand on passe minuit UTC. N'effectue un appel dedie
    (1 credit) que si aucune valeur connue ou force=True."""
    _maybe_reset_quota()
    if force or _remaining["today"] is None:
        try: _get("/shops?shop_name=Etsy")
        except Exception: pass
    return _remaining["today"]

def quota_state():
    """Etat complet du quota pour l'UI (temps reel)."""
    _maybe_reset_quota()
    return {"remaining_today": _remaining["today"],
            "limit": _remaining.get("limit") or DAY_LIMIT_DEFAULT,
            "date": _remaining.get("date")}

# ---------------- classification produit (gratuite) ----------------
# Categories bannies detectees via titre + tags + materials.
BAN_KW = {
    "vetements": ["t-shirt","tshirt"," tee","tee ","shirt","hoodie","sweater","sweatshirt",
                  "cardigan","dress","leggings","apparel","clothing","socks","beanie","scarf",
                  "bodysuit","lingerie","bikini","swimsuit","pajama","pyjama","romper","jumpsuit",
                  "skirt","pants","shorts","blouse","kimono robe","crop top","tank top","onesie"],
    "personnalise": [],  # gere via flags listing (is_personalizable/customizable)
    "digital": ["svg","png ","dxf","clipart","printable","instant download","digital download",
                "pdf pattern","cut file","cricut","glowforge","sublimation png","procreate","template",
                "canva","mockup","spreadsheet","ebook","e-book","notion template","lightroom preset"],
    "stickers": ["sticker","stickers","decal","keychain","key chain","key ring","keyring","patch ",
                 "iron-on","iron on","magnet sheet","vinyl decal","bumper sticker","enamel pin","schlüsselanhänger","porte-cle","porte cle","breloque",
                 "dtf transfer","heat transfer","htv ","screen print transfer","uv dtf"],
    "bijoux": ["necklace","earring","earrings","bracelet"," ring ","pendant","jewelry","jewellery",
               "brooch","anklet","choker","bangle","bague","collier","boucle","cufflink","body chain","nose ring","septum"],
    "electronique": ["neon sign","led light","led strip","led neon","usb","charger","bluetooth","cable",
                     "lamp bulb","smart watch"," speaker","headphone","powerbank","fairy lights"],
    "gadget": ["phone case","phone grip","airpod","popsocket","fidget","gadget","car mount",
               "bottle opener","mouse pad","laptop stand","cable organizer"],
    "croyance": ["psychic","tarot","spell","astrology","ouija","witchcraft","crystal healing","reiki",
                 "spiritual","chakra","manifestation","zodiac","horoscope","pendulum","smudge","sage bundle",
                 "moon phase ritual","pagan","wiccan"],
    "plantes": ["live plant","seeds","seedling","bare root","plant cutting","succulent live","bulbs "],
    "fandom_replica": ["replica","cosplay prop","star wars","harry potter","pokemon","disney",
                       "marvel","anime figure","funko","lightsaber","fan art print"],
    "taxidermie": ["taxidermy","animal skull","real bone","wet specimen","preserved insect","real feather lot"],
}
# Produits trop lourds (port cher) detectes par mots-cles si poids absent.
HEAVY_KW = ["area rug","wool rug","tufted rug","carpet","doormat","door mat","furniture",
            "coffee table","dining table","side table","wooden chair","bench seat","bar stool",
            "cast iron","concrete planter","marble slab","stoneware crock","fire pit",
            "stepping stone","paver","headboard","metal sign"]

def _txt(L):
    return (" ".join([L.get("title","")] + (L.get("tags") or []) + (L.get("materials") or []))).lower()

def classify_listing(L, opts):
    """Retourne (ok: bool, raison: str). 0 appel API."""
    t = _txt(L)
    # personnalise (flags fiables Etsy)
    if opts.get("excl_perso") and (L.get("is_personalizable") or L.get("is_customizable")):
        return False, "personnalise"
    # supply / matiere premiere
    if opts.get("excl_supply") and L.get("is_supply"):
        return False, "supply"
    # vintage / revente
    if opts.get("excl_vintage") and (L.get("when_made") or "").startswith(("19","2000","2001","2002","2003","2004","2005","2006","2007","2008","2009","2010")):
        return False, "vintage"
    # prix mini (eviter le cheap a marge nulle)
    p = L.get("price") or {}
    price = (p.get("amount",0)/p.get("divisor",1)) if p.get("divisor") else 0
    if price and price < opts.get("min_price", 0):
        return False, "trop cheap"
    # poids max (port cher) — item_weight en oz ou lb/g/kg
    w = L.get("item_weight"); wu = (L.get("item_weight_unit") or "").lower()
    if w and opts.get("max_weight_g"):
        grams = w * {"oz":28.35,"lb":453.6,"g":1,"kg":1000,"":28.35}.get(wu, 28.35)
        if grams > opts["max_weight_g"]:
            return False, "trop lourd"
    # lourd par mot-cle
    if opts.get("excl_heavy") and any(k in t for k in HEAVY_KW):
        return False, "lourd (mot-cle)"
    # categories bannies
    for cat in opts.get("ban_categories", []):
        if any(k in t for k in BAN_KW.get(cat, [])):
            return False, cat
    return True, "ok"

# ---------------- enrichissement boutique (1 appel, cache) ----------------
def _fetch_shop_record(shop_id, sample=""):
    """SEULEMENT l'appel reseau /shops/{id} -> rec. Aucune ecriture cache => sur a
    appeler en parallele (ThreadPool). Retourne {error} si echec."""
    key = str(shop_id)
    try:
        m = _get(f"/shops/{shop_id}")
    except Exception as e:
        return {"id": key, "error": str(e)[:50], "sold": None}
    cd = datetime.fromtimestamp(m["create_date"], timezone.utc); now = NOW()
    months = (now.year-cd.year)*12 + (now.month-cd.month) or 1
    sold = m.get("transaction_sold_count",0) or 0
    active = m.get("listing_active_count",1) or 1
    return {"id":key,"name":m.get("shop_name"),
            "url":"https://www.etsy.com/shop/"+(m.get("shop_name") or ""),
            "country":m.get("shop_location_country_iso"),
            "sold":sold,"months":months,"rate":round(sold/months,1),
            "active":active,"digital_pct":round(100*(m.get("digital_listing_count",0) or 0)/max(active,1)),
            "favorers":m.get("num_favorers",0),"reviews":m.get("review_count",0),
            "review_avg":m.get("review_average"),"accepts_custom":m.get("accepts_custom_requests"),
            "sample":sample or ""}

def enrich_shop(shop_id, sample, niche_hint=""):
    cache = _load(); key = str(shop_id)
    if key in cache and cache[key].get("sold") is not None:
        c = dict(cache[key]); c["sample"] = sample or c.get("sample",""); return c
    rec = _fetch_shop_record(shop_id, sample)
    if not rec.get("error"):
        cache[key] = rec; _save(cache)
    return rec

def fetch_shop_titles(shop_id, limit=100):
    """1 appel: recupere TOUS les titres du catalogue (jusqu'a 100) -> niche fiable.
    Renvoie aussi les listing_ids (pour la validation image AliExpress)."""
    try:
        d = _get(f"/shops/{shop_id}/listings/active?limit={limit}&sort_on=score")
    except Exception:
        return []
    out = []
    for L in d.get("results", []):
        t = (L.get("title") or "").strip()
        if t and t not in out:
            out.append(t)
    return out

def fetch_shop_listing_ids(shop_id, n=10):
    """Renvoie les n premiers listing_ids (best-sellers d'abord) d'une boutique. 1 appel."""
    try:
        d = _get(f"/shops/{shop_id}/listings/active?limit={max(n,10)}&sort_on=score")
    except Exception:
        return []
    return [str(L["listing_id"]) for L in d.get("results", []) if L.get("listing_id")][:n]

def fetch_listing_images(listing_ids, per=1, with_prices=False):
    """1 appel batch: {listing_id: [image_urls]} pour jusqu'a 100 listings.
    includes=Images embarque les images (url_570xN). Si with_prices=True, retourne
    (images_dict, {listing_id: prix_float}) pour comparer au prix AliExpress (marge dropship)."""
    if not listing_ids:
        return ({}, {}) if with_prices else {}
    try:
        b = _get("/listings/batch?listing_ids=" + ",".join(listing_ids[:100]) + "&includes=Images")
    except Exception:
        return ({}, {}) if with_prices else {}
    out = {}; prices = {}
    for L in b.get("results", []):
        lid = str(L.get("listing_id"))
        imgs = [im.get("url_570xN") or im.get("url_fullxfull") for im in (L.get("images") or [])]
        imgs = [u for u in imgs if u]
        if imgs:
            out[lid] = imgs[:per]
        p = L.get("price") or {}
        if p.get("divisor"):
            try: prices[lid] = (p.get("amount", 0) or 0) / p["divisor"]
            except Exception: pass
    return (out, prices) if with_prices else out

# ---------------- clustering (niche deduite) ----------------
# Themes avec mots-cles SPECIFIQUES (evite faux positifs type "cat" qui matchait suncatcher).
THEMES = [
    ("Vitrail & suncatchers", ["suncatcher","stained glass","sun catcher","window hanging glass"]),
    ("Tapis & paillassons", ["rug","carpet","doormat","door mat","area mat","tufted rug"]),
    ("Macramé & tentures murales", ["macrame","tapestry","wall hanging","woven wall","fiber art wall"]),
    ("Housses coussin / linge maison", ["pillow cover","cushion cover","pillowcase","throw pillow","duvet","bedding","linen sheet","sofa cover"]),
    ("Sacs (crochet/raffia/perlé)", ["tote bag","handbag","crossbody","raffia bag","beaded bag","clutch","purse","shoulder bag","crochet bag"]),
    ("Bandanas (humain)", ["bandana","head scarf","neck scarf"]),
    ("Accessoires pour animaux", ["dog collar","cat collar","pet bandana","dog bandana","dog bow tie","pet scarf","dog leash","cat toy"]),
    ("Cadres & fleurs pressées", ["pressed flower","dried flower frame","photo frame","picture frame","botanical frame","gallery frame"]),
    ("Miroirs déco", ["mirror","wall mirror","vanity mirror"]),
    ("Plateaux & vide-poches", ["serving tray","decorative tray","trinket dish","catchall tray"]),
    ("Vases & céramique déco", ["vase","ceramic ","pottery","stoneware","planter pot"]),
    ("Encens & diffuseurs", ["incense holder","incense burner","ash catcher","palo santo holder"]),
    ("Paniers & rangement tressé", ["wicker basket","rattan basket","seagrass basket","storage basket","woven basket"]),
    ("Accessoires cheveux", ["hair clip","claw clip","scrunchie","hair bow","headband"]),
    ("Cake toppers & déco fête", ["cake topper","party banner","party favor","cupcake topper"]),
    ("Bain & savon", ["soap bar","bath bomb","shower steamer","bath salt"]),
    ("Bougies déco", ["candle","wax melt"]),
    ("Résine & époxy", ["resin ","epoxy ","resin art"]),
    ("Crochet & peluches", ["amigurumi","crochet plush","stuffed animal","plushie","felted"]),
    ("Déco murale (affiches/art)", ["wall art","art print","poster","canvas print"]),
    ("Déco bois / objets bois", ["wooden ","wood carved","carved wood","wood figurine","bamboo "]),
]
# Mappe chaque THEME (clustering mot-cle) vers la TAXONOMIE FIXE => meme niches que
# l'IA => regroupement coherent dans TOUS les modes (cache/scrape/api, avec ou sans IA).
THEME_TO_TAXO = {
    "Vitrail & suncatchers": "Decoration murale",
    "Tapis & paillassons": "Tapis & paillassons",
    "Macramé & tentures murales": "Decoration murale",
    "Housses coussin / linge maison": "Coussins & linge de maison",
    "Sacs (crochet/raffia/perlé)": "Sacs & pochettes",
    "Bandanas (humain)": "Autre deco maison",
    "Accessoires pour animaux": "Accessoires pour animaux",
    "Cadres & fleurs pressées": "Decoration murale",
    "Miroirs déco": "Decoration murale",
    "Plateaux & vide-poches": "Plateaux & vide-poches",
    "Vases & céramique déco": "Vases & ceramique deco",
    "Encens & diffuseurs": "Bougies & senteurs",
    "Paniers & rangement tressé": "Paniers & rangement tresse",
    "Accessoires cheveux": "Accessoires cheveux",
    "Cake toppers & déco fête": "Deco de fete & cake toppers",
    "Bain & savon": "Bain & savon",
    "Bougies déco": "Bougies & senteurs",
    "Résine & époxy": "Autre deco maison",
    "Crochet & peluches": "Peluches & crochet deco",
    "Déco murale (affiches/art)": "Decoration murale",
    "Déco bois / objets bois": "Objets deco en bois",
}
PERSO_TITLE_KW = ["personalized","personalised","custom name","custom text","monogram",
                  "your name","your photo","your text","add your","engraved name","custom photo","personalisiert","personnalise","prenom","mit name","gravur"]

# Marqueurs PRODUIT DIGITAL (fichier telechargeable, patron, asset jeu) — detection
# robuste sur titre. Toujours appliquee en mode scrape (boutique rejetee si la majorite
# du catalogue est digitale).
DIGITAL_TITLE_KW = [
    "digital download","instant download","instant dl","downloadable","printable","print at home",
    "svg","png file","dxf","eps file","clipart","cut file","cricut","glowforge","silhouette cut",
    "sublimation","procreate","canva template","editable template","template","mockup","mock-up",
    "ebook","e-book","pdf","spreadsheet","notion template","lightroom preset",
    "embroidery design","machine embroidery","embroidery file","pes file","applique design",
    "crochet pattern","knitting pattern","amigurumi pattern","sewing pattern","pattern pdf","pdf pattern",
    "mlo","fivem","gta","game asset","3d model","stl file","stl ","blender file","fbx",
    "magazine","newspaper","newsletter pdf","wall art printable","digital art","digital paper",
]

def shop_titles(r):
    return r.get("titles") or ([r["sample"]] if r.get("sample") else [])

def perso_ratio(titles):
    if not titles: return 0.0
    n = sum(1 for t in titles if any(k in t.lower() for k in PERSO_TITLE_KW))
    return n / len(titles)

def digital_ratio(titles):
    """Part du catalogue qui ressemble a un produit digital/fichier/patron."""
    if not titles: return 0.0
    n = sum(1 for t in titles if any(k in t.lower() for k in DIGITAL_TITLE_KW))
    return n / len(titles)

def cat_ratio(titles, kws):
    """Part des titres contenant un mot-cle de la categorie."""
    if not titles: return 0.0
    n = sum(1 for t in titles if any(k in t.lower() for k in kws))
    return n / len(titles)

# Seuils PROPORTION (lucidite): on rejette une boutique seulement si la categorie
# DOMINE le catalogue, pas sur 1 titre isole. Avant: 1 mot-cle dans 100 titres tuait
# toute la boutique => 3500 trouvees, 15 gardees. Maintenant ratio-base.
PERSO_REJECT = 0.55      # >55% du catalogue personnalise
DIGITAL_REJECT = 0.55    # >55% digital
CAT_REJECT = 0.50        # >50% d'une categorie bannie (bijoux, vetements...)
HEAVY_REJECT = 0.50

def catalog_reject(titles, f):
    """Decision niveau BOUTIQUE, basee sur la PROPORTION du catalogue (intelligent).
    Retourne (reject: bool, raison). Une boutique n'est rejetee que si la mauvaise
    categorie domine reellement son catalogue."""
    if not titles:
        return False, ""
    if f.get("exclude_perso", True) and perso_ratio(titles) >= PERSO_REJECT:
        return True, "perso"
    if digital_ratio(titles) >= DIGITAL_REJECT:
        return True, "digital"
    if f.get("exclude_heavy", True) and cat_ratio(titles, HEAVY_KW) >= HEAVY_REJECT:
        return True, "lourd"
    for cat in f.get("exclude_categories", []):
        if cat_ratio(titles, BAN_KW.get(cat, [])) >= CAT_REJECT:
            return True, cat
    return False, ""

# mots a ignorer pour l'auto-nommage (marketing / filler / generiques)
STOP = set(("the a an for with and of to in on your you my our handmade hand made gift gifts "
    "custom personalized set new cute boho style modern home decor decoration design unique "
    "made best sale shop large small mini big size color colour piece pieces pack bundle "
    "perfect lovely beautiful quality premium christmas valentine mothers fathers day birthday "
    "women men kids baby her him mom dad uk us usa free shipping etsy original natural ").split())
PRODUCT_HINT = set((
    "rug pillow bag candle mirror tray vase basket frame bandana suncatcher mat "
    "tapestry macrame wreath coaster planter holder bowl plate cushion blanket throw clip "
    "earrings necklace soap topper sign board ornament figurine sculpture lamp shade light "
    "doormat cover pouch purse tote clutch shawl scarf hat cap stand organizer box dish "
    "magnet keychain sticker print poster art painting portrait jewelry bracelet ring pendant "
    "mug cup bottle tumbler jar plate bowl spoon cutting coaster trivet apron towel napkin "
    "blanket quilt throw runner placemat curtain pillowcase duvet sheet "
    "necklace brooch anklet hairclip scrunchie headband barrette "
    "wallet cardholder lanyard pin patch badge "
    "keychain bookmark notebook journal planner card "
    "plush toy doll figure model kit puzzle game "
    "soap candle balm scrub oil perfume incense diffuser "
    "wallart canvas decal mural banner garland bunting "
    "shelf hook rack hanger stand easel pedestal "
    "clock watch frame album "
    "bowl mug vase pot planter saucer ").split())

def _phrases(titles):
    """Compte les bigrammes/mots produit recurrents dans le catalogue."""
    import re as _re
    from collections import Counter
    uni, bi = Counter(), Counter()
    for t in titles:
        words = [w for w in _re.findall(r"[a-z]+", t.lower()) if w not in STOP and len(w) > 2]
        for w in words:
            uni[w] += 1
        for a, b in zip(words, words[1:]):
            bi[a + " " + b] += 1
    return uni, bi

def auto_niche(titles):
    """Nom de niche derive des mots produit dominants. TOUJOURS un nom (jamais 'Autres'):
    chaque boutique vend forcement quelque chose => on nomme par le produit dominant."""
    uni, bi = _phrases(titles)
    n = max(1, len(titles))
    # 1) bigramme avec mot-produit, recurrent (>=25%)
    for phrase, c in bi.most_common(20):
        if c >= max(2, n * 0.25) and any(h in phrase.split() for h in PRODUCT_HINT):
            return phrase.title(), round(c / n, 2)
    # 2) mot-produit le plus frequent (seuil bas: 2 occurrences)
    for w, c in uni.most_common(30):
        if w in PRODUCT_HINT and c >= 2:
            return w.title() + "s", round(c / n, 2)
    # 3) bigramme le plus frequent quel qu'il soit
    for phrase, c in bi.most_common(5):
        if c >= 2:
            return phrase.title(), round(c / n, 2)
    # 4) mot le plus frequent (toujours quelque chose)
    if uni:
        w, c = uni.most_common(1)[0]
        return w.title() + "s", round(c / n, 2)
    return "Divers artisanat", 0.0

def niche_of(titles):
    """Renvoie (niche_taxonomie, confiance). Toujours une valeur de NICHE_TAXONOMY =>
    regroupement coherent avec l'IA. 1) theme mot-cle -> mappe sur taxonomie. 2) sinon
    auto-nommage du catalogue -> snap sur taxonomie."""
    if not titles: return "Autre deco maison", 0.0
    low = [t.lower() for t in titles]
    best, best_hits = None, 0
    for name, kws in THEMES:
        hits = sum(1 for t in low if any(kw in t for kw in kws))
        if hits > best_hits:
            best, best_hits = name, hits
    conf = best_hits / len(titles)
    if best and (best_hits >= 2 or conf >= 0.3):
        return THEME_TO_TAXO.get(best, "Autre deco maison"), round(conf, 2)
    # pas de theme net -> nom derive du catalogue, ramene dans la taxonomie
    raw, c = auto_niche(titles)
    return snap_niche(raw), c

def aliexpress_url(text):
    """Lien recherche AliExpress (mots-cles produit) pour verifier la dispo dropshipping."""
    import re as _re
    words = [w for w in _re.findall(r"[A-Za-z]+", text or "") if w.lower() not in STOP and len(w) > 2]
    return "https://www.aliexpress.com/wholesale?SearchText=" + urllib.parse.quote(" ".join(words[:4]))

def _ensure_labels(shops):
    """Calcule (une fois) le label de niche + bouton AliExpress sur chaque boutique.
    Stocke dans r['_niche'] pour reutilisation (diversification + clustering)."""
    for r in shops:
        if not r.get("_niche"):
            if r.get("ai_niche"):
                r["_niche"] = r["ai_niche"]   # niche LIBRE deduite par l'IA (deja normalisee)
            else:
                lab, conf = niche_of(shop_titles(r))
                r["_niche"] = lab; r["niche_conf"] = conf
        if not r.get("ali_url"):
            r["ali_url"] = aliexpress_url(r.get("sample", ""))

CATCHALL = "Autre deco maison"   # niche fourre-tout: toujours reléguée en dernier

def cluster(shops):
    _ensure_labels(shops)
    g = {}
    for r in shops:
        g.setdefault(r["_niche"], []).append(r)
    out = [{"niche": n, "count": len(l), "shops": sorted(l, key=lambda x: -x["rate"])} for n, l in g.items()]
    # vraies niches d'abord (par taille), le fourre-tout 'Autre deco maison' en dernier
    out.sort(key=lambda x: (x["niche"] == CATCHALL, -x["count"])); return out

def _diverse_slice(shops, target):
    """Coupe a EXACTEMENT `target` boutiques en round-robin entre niches: evite que
    le resultat soit domine par toujours la meme niche. shops deja trie par rate."""
    if not target or target <= 0 or len(shops) <= target:
        return shops
    from collections import OrderedDict
    buckets = OrderedDict()
    for s in shops:
        buckets.setdefault(s.get("_niche", "?"), []).append(s)
    if CATCHALL in buckets:                 # fourre-tout pioche en dernier
        buckets.move_to_end(CATCHALL)
    out = []
    while len(out) < target and any(buckets.values()):
        for lab in list(buckets.keys()):
            if buckets[lab]:
                out.append(buckets[lab].pop(0))
                if len(out) >= target:
                    break
    return out

def finalize(res, target=0, min_per_niche=1, diversify=True):
    """Etape finale commune a tous les modes.

    min_per_niche=n>1 (ex: 5) est un MINIMUM, pas un plafond: on ne garde que les
    niches ayant AU MOINS n boutiques, et on affiche TOUTES leurs boutiques (pas
    seulement n). `target` plafonne le total: on empile les niches (les meilleures
    d'abord) tant qu'on ne depasse pas la cible.

    Si min_per_niche<=1: round-robin entre niches puis coupe a EXACTEMENT target."""
    shops = res.get("shops", [])
    _ensure_labels(shops)
    try: n = max(1, int(min_per_niche or 1))
    except Exception: n = 1

    all_shops = list(shops)
    if n > 1:
        # groupe par niche, garde niches a >= n, affiche TOUTES leurs boutiques
        g = {}
        for s in shops:
            g.setdefault(s["_niche"], []).append(s)
        full = []
        for lab, lst in g.items():
            if len(lst) >= n:                 # minimum n => la niche est retenue
                lst.sort(key=lambda x: -x["rate"])
                full.append((lab, lst, lst[0]["rate"]))
        # vraies niches d'abord (par ventes/mois de leur tete), fourre-tout en dernier
        full.sort(key=lambda t: (t[0] == CATCHALL, -t[2]))
        # VARIETE + minimum: chaque niche retenue recoit d'abord ses n meilleures
        # (garantit le minimum), puis on distribue le reste en round-robin entre niches
        # jusqu'a la cible => plusieurs niches affichees, pas une seule qui monopolise.
        if target and target > 0:
            max_niches = max(1, target // n)          # nb de niches qui tiennent dans la cible
            chosen = full[:max_niches]
        else:
            chosen = full
        from collections import deque
        queues = [(lab, deque(lst)) for lab, lst, _r in chosen]
        out = []
        # passe 1: minimum n par niche
        for lab, q in queues:
            for _ in range(min(n, len(q))):
                out.append(q.popleft())
        # passe 2: round-robin du reste jusqu'a la cible
        while (not target or target <= 0 or len(out) < target) and any(q for _, q in queues):
            for lab, q in queues:
                if q:
                    out.append(q.popleft())
                    if target and target > 0 and len(out) >= target:
                        break
        if not out and full:             # target < n : garde au moins 1 niche pleine
            out = full[0][1]
        shops = out
    else:
        shops.sort(key=lambda x: -x["rate"])
        if target and target > 0:
            shops = _diverse_slice(shops, target) if diversify else shops[:target]

    # FALLBACK gracieux: si min_per_niche>1 ne laisse AUCUNE niche pleine mais qu'on a
    # quand meme des boutiques valides, on les montre quand meme (sinon ecran vide
    # frustrant). On signale que la contrainte n/niche n'a pas pu etre respectee.
    if not shops and all_shops:
        all_shops.sort(key=lambda x: -x["rate"])
        shops = _diverse_slice(all_shops, target) if (target and target > 0) else all_shops
        res["min_per_niche_relaxed"] = True

    res["shops"] = shops
    res["matched"] = len(shops)
    res["clusters"] = cluster(shops)
    return res

# compat: ancien nom
def apply_min_per_niche(res, n):
    return finalize(res, target=0, min_per_niche=n)

# ---------------- recherche base locale (0 API) ----------------
def search_cache(filters=None, keyword=""):
    """Filtre la base locale de boutiques deja enrichies. ZERO requete API.
    Renvoie potentiellement des centaines de boutiques instantanement."""
    f = filters or {}
    kw_en, _tr = resolve_keyword(keyword)   # FR -> EN (Etsy/catalogues en anglais)
    kw_en = kw_en.strip().lower()
    f["_query"] = kw_en          # pertinence IA (jugement semantique vs la recherche)
    # seuil de pertinence: au moins 50% des mots-cles forts presents dans le catalogue
    rel_min = float(f.get("relevance_min", 0.35)) if kw_en else 0.0
    cache = _load()
    shops = []
    for rec in cache.values():
        if rec.get("sold") is None or rec.get("error"):
            continue
        titles = shop_titles(rec)
        # FILTRE PERTINENCE: la boutique doit VRAIMENT vendre ce qui est cherche
        if kw_en and keyword_relevance(titles, kw_en) < rel_min:
            continue
        # rejet PROPORTIONNEL (intelligent): perso/digital/categorie bannie/lourd
        # seulement si ca DOMINE le catalogue.
        rej, _ = catalog_reject(titles, f)
        if rej:
            continue
        # filtres boutique
        if rec["rate"] < f.get("min_rate", 0): continue
        if rec["months"] > f.get("max_age_months", 999): continue
        if rec["months"] < f.get("min_age_months", 0): continue
        if rec["sold"] < f.get("min_sold", 0): continue
        if f.get("exclude_digital", True) and rec.get("digital_pct", 0) >= 50: continue
        if kw_en:                          # affiche un produit qui matche le mot-cle
            rec = dict(rec); rec["sample"] = match_sample(titles, kw_en)
        shops.append(rec)
    shops.sort(key=lambda x: -x["rate"])   # best-sellers d'abord (ventes/mois)
    total = len(shops)
    # IA (GLM gratuit): juge/repartit sur un POOL limite (les meilleurs candidats) pour
    # ne pas exploser le nombre d'appels. Puis finalize coupe a la cible exacte.
    ai_used = False
    tc = f.get("target_count", 0)
    if f.get("use_ai") and ai_available() and shops:
        pool_n = max(tc * 2, 40) if tc else 120
        pool, rest = shops[:pool_n], shops[pool_n:]
        pool, ai_used = ai_enrich_shops(pool, f)
        shops = pool + rest
    res = {
        "source": "cache",
        "api_used": 0,
        "ai_used": ai_used,
        "ai_available": ai_available(),
        "ai_model": ai_model_name(),
        "db_size": len(cache),
        "available": total,
        "matched": len(shops),
        "quota_remaining": _remaining["today"],
        "clusters": [],
        "shops": shops,
    }
    # coupe a EXACTEMENT target_count + diversifie les niches
    return finalize(res, target=tc, min_per_niche=f.get("min_per_niche", 1))

# ---------------- scraping navigateur (0 API) ----------------
def _sample_is_bad(sample, f):
    """Pre-filtre GRATUIT sur le titre echantillon vu en page de recherche.
    Evite de charger la page boutique pour un produit clairement digital/banni/perso.
    => moins de pages chargees, scrape bien plus rapide."""
    if not sample:
        return False  # pas d'info => on charge pour decider
    low = sample.lower()
    if any(k in low for k in DIGITAL_TITLE_KW):
        return True
    if f.get("exclude_perso", True) and any(k in low for k in PERSO_TITLE_KW):
        return True
    for cat in f.get("exclude_categories", []):
        if any(k in low for k in BAN_KW.get(cat, [])):
            return True
    if f.get("exclude_heavy", True) and any(k in low for k in HEAVY_KW):
        return True
    return False

def run_scrape(keyword="", target_count=30, filters=None, progress=None, stop=None):
    """Scrape Etsy (navigateur, 0 credit API): trouve des boutiques, scrape TOUT leur
    catalogue (tous les titres), filtre et repartit par niche.
    Optimise: pre-filtre les samples (skip digital/banni avant de charger la boutique),
    charge les pages boutiques EN PARALLELE (ThreadPool), rejette toujours les catalogues
    a majorite digitale."""
    import scraper
    if not scraper.SCRAPLING_OK:
        return {"error": "scrapling non installe", "source": "scrape", "shops": [], "clusters": []}
    keyword, _kw_tr = resolve_keyword(keyword)   # nom de niche FR -> mots-cles EN
    f = filters or {}
    cache = _load()
    # CURSEUR PERSISTANT (reprise): chaque run de scrape repart la ou le precedent s'est
    # arrete (par mot-cle) => on scanne de NOUVELLES pages au lieu de re-scraper page 1.
    # Combine au cache qui accumule sans doublon => on atteint 1000 sur plusieurs runs.
    scrape_ckey = "scrape:" + (keyword.strip().lower() or "_all")
    pg = _cursor_get(scrape_ckey)
    shops = []; seen = set(); scraped = 0; found_total = 0; skipped_pre = 0

    def keep(rec):
        titles = shop_titles(rec)
        rej, _ = catalog_reject(titles, f)   # rejet proportionnel (pas sur 1 titre isole)
        if rej: return False
        if rec["rate"] < f.get("min_rate", 0): return False
        if rec["months"] > f.get("max_age_months", 999): return False
        if rec["months"] < f.get("min_age_months", 0): return False
        return True

    def build(name, sample, d):
        # avant: on jetait toute boutique sans "X months on Etsy" => 0 boutique si Etsy
        # change le HTML. Maintenant: tant qu'on a les ventes, on garde (age inconnu=12).
        if d.get("error") or d.get("sold") is None:
            return None
        mo = d.get("months") or 12
        return {"id": name, "name": name, "url": "https://www.etsy.com/shop/" + name,
                "country": None, "sold": d["sold"], "months": mo,
                "rate": round(d["sold"]/mo, 1), "active": 1, "digital_pct": 0,
                "favorers": 0, "reviews": 0, "review_avg": None, "accepts_custom": None,
                "sample": (d["titles"][0] if d["titles"] else sample),
                "titles": d["titles"] or ([sample] if sample else []),
                "price": d.get("price"),   # prix vente median (JSON-LD) -> marge dropship
                "images": d.get("images") or []}   # images produit scrapees (validation dropship sans API)

    empty_streak = 0; nonew_streak = 0
    # budget anti-boucle: au pire on charge ~25 boutiques par cible avant d'abandonner.
    max_scraped = int(f.get("max_scraped", 0)) or max(target_count * 25, 400)
    # Page vide = block Datadome transitoire. On retente 1 fois avec une courte pause, mais
    # on ABANDONNE VITE (5 echecs) pour rendre les resultats deja trouves au lieu de faire
    # patienter l'utilisateur ~4min. Avant: 12 echecs avec backoff jusqu'a 12s = trop long.
    empty_limit = int(f.get("empty_streak_limit", 0)) or 3
    # "plus de NOUVEAUX resultats": Etsy renvoie les MEMES boutiques (deja vues) => le stock
    # pertinent est epuise. On coupe apres 3 paquets sans aucune nouvelle boutique => rend
    # direct au lieu de scanner du vide.
    nonew_limit = int(f.get("nonew_limit", 0)) or 3
    # Nb de pages de recherche fetchees EN PARALLELE par tour (chacune ~48 boutiques).
    # Gros gain vitesse: avant 1 page/tour avec attente ~4s bloquante. Plus haut = plus
    # vite mais + de risque 403 Datadome => 5 est un bon compromis.
    chunk = int(f.get("search_pages_chunk", 0)) or 5
    PAGE_CAP = int(f.get("page_cap", 0) or 0) or max(250, target_count // 3 + 60)
    samples = {}   # name -> sample (pour le build)
    # SUR-ECHANTILLONNAGE (scrape = 0 credit => on peut en collecter large): l'IA et la
    # validation AliExpress filtrent APRES la boucle. On collecte plus de candidats pour
    # qu'il RESTE >= target_count boutiques EN NICHE apres filtrage.
    gate_on = bool(f.get("use_ai", True) or f.get("validate_ali"))
    collect_target = (target_count * 2 if gate_on else target_count)
    # STAGNATION: si on scrape beaucoup SANS garder de nouvelle boutique (niche epuisee dans
    # la fenetre courante OU Etsy bloque/reboucle), on s'arrete et on REND DIRECT au lieu de
    # tourner en boucle. Plafond de boutiques scrapees depuis le dernier "keep".
    # avec proxies le scraping est rapide + non bloque => on tolere plus de scan sans keep
    # pour atteindre la cible (sinon on s'arrete trop tot sur une niche a faible rendement).
    try:
        import scraper as _scr; _has_proxies = bool(getattr(_scr, "_PROXIES", []))
    except Exception:
        _has_proxies = False
    stall_limit = int(f.get("scrape_stall", 0)) or (300 if _has_proxies else 120)
    scraped_at_last_keep = 0
    # boucle jusqu'a collect_target, epuisement Etsy, ou budget atteint.
    while len(shops) < collect_target and pg < PAGE_CAP and scraped < max_scraped and not _stopped(stop):
        found = scraper.scrape_search_shops(keyword, pages=chunk, page_start=pg + 1)
        pg += chunk
        if not found:
            # 1 seule courte pause + 1 retry. Si ca rebloque, on N'INSISTE PAS: on compte
            # l'echec et on s'arrete vite (empty_limit bas) => bilan rendu IMMEDIATEMENT au
            # lieu de faire patienter sur des proxies epuises/bloques.
            time.sleep(2)
            found = scraper.scrape_search_shops(keyword, pages=chunk, page_start=pg - chunk + 1)
        if not found:
            empty_streak += 1
            if empty_streak >= empty_limit: break   # bloque => on rend ce qu'on a, direct
            continue
        empty_streak = 0
        found_total += len(found)
        # pre-filtre + dedup => liste de boutiques a charger
        batch = []
        for name, sample in found:
            if name in seen: continue
            seen.add(name)
            if _sample_is_bad(sample, f):
                skipped_pre += 1; continue
            samples[name] = sample
            batch.append(name)
        # batch vide = que des dupes/filtrees = aucune NOUVELLE boutique => stock epuise.
        # Apres nonew_limit paquets ainsi, on s'arrete et on rend les resultats direct.
        if not batch:
            nonew_streak += 1
            if nonew_streak >= nonew_limit: break
            continue
        nonew_streak = 0
        # chargement PARALLELE du batch (1 navigateur, pool de pages)
        results = scraper.scrape_shops_batch(batch)
        for name in batch:
            scraped += 1
            rec = build(name, samples.get(name, ""), results.get(name, {}))
            if rec is None: continue
            cache_upsert(cache, rec)   # enregistre toute boutique trouvee, sans doublon
            if keep(rec):
                shops.append(rec); scraped_at_last_keep = scraped
            if progress: progress(len(shops), scraped)
            if len(shops) >= collect_target: break
        _save(cache)
        # stagnation: trop de boutiques scrapees depuis le dernier keep => on rend direct.
        if scraped - scraped_at_last_keep >= stall_limit:
            break
    # memorise ou on s'est arrete pour REPRENDRE au prochain run (pages suivantes).
    # Si on a depasse le stock Etsy (~PAGE_CAP), on reboucle au debut.
    _cursor_set(scrape_ckey, 0 if pg >= PAGE_CAP else pg)
    shops.sort(key=lambda x: -x["rate"])
    # raffinage IA: l'IA lit TOUS les titres de fiches produit de chaque boutique
    # (deja scrapes dans rec["titles"]) et DEDUIT le nom de niche depuis les titres
    # majoritaires (ex: beaucoup de titres "fleurs" => niche "Plantes & fleurs").
    # Sans ca, le scrape retombait sur le clustering mot-cle => tout en "Autre deco maison".
    if "_query" not in f:
        f["_query"] = _kw_tr or keyword
    if keyword.strip():
        f.setdefault("_query_raw", keyword.strip())   # phrase brute => match semantique fidele
    ai_used = False
    if f.get("use_ai", True) and ai_available() and shops:
        shops, ai_used = ai_enrich_shops(shops, f)
    # validation dropship AliExpress (opt-in), comme en mode discovery
    ali_used = False
    if f.get("validate_ali") and shops:
        ali_used = True
        nprod = int(f.get("ali_products", 10) or 10)
        minm = int(f.get("ali_min_match", 2) or 2)
        validate_shops_ali(shops, nprod, minm, stop=stop)
        if f.get("ali_gate", True) and not any(s.get("ali_blocked") for s in shops) and not _stopped(stop):
            # CONSENSUS: on ne garde que les boutiques ou Lens A TROUVE les produits sur
            # AliExpress ET (si l'IA a juge) ou l'IA estime le produit revendable. Coupe les
            # faux positifs (Lens a remonte un lien hasardeux mais produit pas vraiment sourcable).
            shops = [s for s in shops if (s.get("dropship_confirmed")
                     if s.get("dropship_confirmed") is not None else s.get("ali_validated"))]
    res = {"source": "scrape", "api_used": 0, "scraped": scraped,
           "found": found_total, "skipped_pre": skipped_pre, "matched": len(shops),
           "ai_used": ai_used, "ali_used": ali_used, "ai_available": ai_available(),
           "quota_remaining": _remaining["today"], "clusters": [], "shops": shops}
    # coupe a EXACTEMENT target_count + diversifie les niches
    return finalize(res, target=target_count, min_per_niche=f.get("min_per_niche", 1))

# ---------------- completer catalogues manquants ----------------
def complete_catalogs(limit=50):
    """Recupere les titres des boutiques qui n'ont qu'1 titre en base (mono-titre),
    pour fiabiliser leur niche. Cout = 1 appel API / boutique completee."""
    cache = _load()
    todo = [sid for sid, r in cache.items()
            if r.get("sold") is not None and len(r.get("titles") or [r.get("sample","")]) <= 1]
    done = 0
    for sid in todo[:limit]:
        ts = fetch_shop_titles(sid)
        if ts:
            cache[sid]["titles"] = ts
            cache[sid]["sample"] = ts[0]
            done += 1
        time.sleep(0.18)
    _save(cache)
    return {"completed": done, "remaining_monotitle": max(0, len(todo) - done),
            "api_used": done, "quota_remaining": _remaining["today"]}

# ---------------- export CSV ----------------
def export_csv(filters=None, keyword=""):
    res = search_cache(filters, keyword)
    rows = ["niche,boutique,pays,ventes_par_mois,ventes_totales,age_mois,confiance,url,produit"]
    for cl in res["clusters"]:
        for s in cl["shops"]:
            prod = (s.get("sample") or "").replace('"', "'")[:80]
            rows.append(",".join([
                '"' + cl["niche"] + '"', s.get("name",""), s.get("country") or "",
                str(s["rate"]), str(s["sold"]), str(s["months"]),
                str(s.get("niche_conf","")), s.get("url",""), '"' + prod + '"']))
    return "\n".join(rows)

# ---------------- pipeline efficace ----------------
def run_discovery(keyword="", target_count=100, max_api=500, filters=None, progress=None, stop=None):
    """
    Cherche jusqu'a `target_count` bonnes boutiques. Boucle sur les pages de
    nouveautes (sort_on=created => produits recents => boutiques jeunes), filtre
    les produits gratuitement, enrichit les survivants (1 credit/boutique) et
    s'arrete des qu'on atteint la cible OU le budget API `max_api`.
    Resultat trie par best-seller (ventes/mois).
    """
    keyword, _kw_tr = resolve_keyword(keyword)   # nom de niche FR -> mots-cles EN
    f = filters or {}
    opts = {
        "excl_perso": f.get("exclude_perso", True),
        "excl_supply": f.get("exclude_supply", True),
        "excl_vintage": f.get("exclude_vintage", True),
        "excl_heavy": f.get("exclude_heavy", True),
        "min_price": f.get("min_price", 0),
        "max_weight_g": f.get("max_weight_g", 0),
        "ban_categories": f.get("exclude_categories", []),
    }
    cache = _load()
    shops = []; processed = set()
    listing_calls = 0; kept_listings = 0; seen_listings = 0; enriched_calls = 0
    # pertinence: le catalogue de la boutique doit correspondre au mot-cle (deja traduit EN)
    kw_rel = keyword.strip().lower()
    # gate dominance 0.35: le catalogue doit etre MAJORITAIREMENT sur le mot distinctif
    # (ex "desk") pour que la niche soit COHERENTE avec la recherche. A 0.2 trop de
    # boutiques hors-sujet passaient ("organisateur divers", "marqueurs" quand on cherche
    # "desk"). Compromis assume: precision > quantite (on perd quelques boutiques desk
    # eparses, mais on ne renvoie plus de niches incoherentes). Ajustable via relevance_min.
    rel_min = float(f.get("relevance_min", 0.25)) if kw_rel else 0.0
    rel_strong = _strong_tokens(kw_rel) if kw_rel else []   # mots distinctifs (pre-filtre gratuit)
    f["_query"] = kw_rel         # pertinence IA (jugement semantique vs la recherche)
    f["_query_raw"] = (keyword or "").strip()   # phrase brute tapee (semantique fidele)
    # AUTO-IA SUR MOT-CLE: si un mot-cle est tape et qu'une cle IA est dispo, on ACTIVE
    # le jugement IA meme si la case n'est pas cochee. Le filtre mot-cle deterministe est
    # souple (laisse passer des boutiques limites); l'IA, elle, LIT TOUT le catalogue de
    # chaque boutique et tranche si ca correspond vraiment a la recherche => coherence.
    # (Le cache de verdicts rend les runs suivants quasi-instantanes.)
    if kw_rel and ai_available() and not f.get("use_ai"):
        f["use_ai"] = True
        f.setdefault("_ai_auto", True)
    # ROTATION: chaque run repart la ou le precedent s'est arrete (par mot-cle) =>
    # on scanne de NOUVELLES pages de nouveautes => on ne retombe plus toujours sur
    # les memes boutiques / memes niches.
    ckey = "disc:" + (keyword.strip().lower() or "_all")
    # WRAP: l'API Etsy /listings/active plafonne l'offset (~12000). Sans borne, le
    # curseur grimpait a l'infini => offset trop grand => 0 resultat => "aucune boutique".
    MAX_PAGE = 100   # offset max ~10000 (limite Etsy)
    start_pg = _cursor_get(ckey)
    if start_pg >= MAX_PAGE:
        start_pg = 0          # on reboucle au debut
    pg = start_pg
    exhausted = False; reset_tried = False; fail_streak = 0
    # ANTI-GASPILLAGE credits: si on a deja depense un gros budget SANS trouver une
    # seule boutique (filtres trop stricts / mot-cle sans jeunes boutiques), inutile
    # de cramer les 500 credits => on s'arrete tot et on previent l'utilisateur.
    no_match_budget = int(f.get("no_match_budget", 0)) or min(max_api, 150)
    aborted_no_match = False
    last_match_credits = 0   # credits depenses au moment de la derniere boutique gardee
    # SUR-ECHANTILLONNAGE: l'IA (match) et la validation AliExpress filtrent APRES la boucle.
    # Pour qu'il RESTE >= target_count boutiques EN NICHE apres ce filtrage, on collecte plus
    # de candidats quand un gate est actif (sinon on collecte target et le gate en jette =>
    # resultat final < target). Additif + borne pour ne pas exploser les credits.
    gate_on = bool(f.get("use_ai") or f.get("validate_ali"))
    collect_target = min(target_count * 4, target_count + 25) if gate_on else target_count
    while len(shops) < collect_target and pg < start_pg + 120 and pg < MAX_PAGE + 5 \
          and (listing_calls + enriched_calls) < max_api and not _stopped(stop):
        q = f"?limit=100&offset={pg*100}&sort_on=created&sort_order=down"
        if keyword.strip(): q += "&keywords=" + urllib.parse.quote(keyword.strip())
        try:
            d = _get("/listings/active" + q); listing_calls += 1; fail_streak = 0
        except Exception:
            # offset trop grand ou erreur API: on retente depuis 0 (une fois)
            if not reset_tried and pg > 0:
                reset_tried = True; start_pg = 0; pg = 0
                _cursor_set(ckey, 0); continue
            # erreur ponctuelle (apres retries): on saute la page sans tuer le run
            fail_streak += 1; pg += 1
            if fail_streak >= 3:
                break          # 3 pages KO d'affilee => vrai probleme, on arrete
            continue
        pg += 1
        results = d.get("results", [])
        if not results:
            # page vide: si on avait demarre loin (curseur), on retente depuis 0 (1 fois)
            if not reset_tried and start_pg > 0:
                reset_tried = True; start_pg = 0; pg = 0
                _cursor_set(ckey, 0); continue
            exhausted = True   # plus de nouveautes => on bouclera au debut au prochain run
            break
        # 1) filtre produit GRATUIT + collecte des boutiques NOUVELLES de la page
        page_jobs = []            # (sid, sample) a enrichir
        cached_recs = []          # boutiques deja en cache (0 API)
        for L in results:
            seen_listings += 1
            ok, _ = classify_listing(L, opts)        # filtre produit GRATUIT
            if not ok: continue
            # PRE-FILTRE PERTINENCE GRATUIT: le mot-cle search d'Etsy est flou (il matche
            # large). Avant de payer 1 credit pour enrichir la boutique, on exige que le
            # titre du produit qui a matche contienne le mot DISTINCTIF (ex "desk"). Coupe
            # les boutiques hors-sujet (etageres, boites bois...) AVANT de cramer le credit
            # => meilleur ratio + resultats vraiment lies a la recherche.
            title_l = (L.get("title") or "").lower()
            if rel_strong and not any(w in title_l for w in rel_strong):
                continue
            kept_listings += 1
            sid = str(L.get("shop_id") or "")
            if not sid or sid in processed: continue
            processed.add(sid)
            sample = L.get("title", "")[:90]
            if sid in cache and cache[sid].get("sold") is not None:
                c = dict(cache[sid]); c["sample"] = sample or c.get("sample", "")
                cached_recs.append(c)
            else:
                page_jobs.append((sid, sample))
        # 2) enrichissement PARALLELE des nouvelles boutiques (gros gain vitesse)
        budget = max_api - (listing_calls + enriched_calls)
        page_jobs = page_jobs[:max(budget, 0)]
        fetched = []
        if page_jobs:
            with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as ex:
                fetched = list(ex.map(lambda j: _fetch_shop_record(j[0], j[1]), page_jobs))
            for r in fetched:
                if not r.get("error"):
                    enriched_calls += 1
                    cache[r["id"]] = r
            _save(cache)
        # 3) filtres BOUTIQUE
        keep_recs = []
        for rec in cached_recs + fetched:
            if rec.get("error") or rec.get("sold") is None: continue
            if rec["rate"] < f.get("min_rate", 0): continue
            if rec["months"] > f.get("max_age_months", 999): continue
            if rec["months"] < f.get("min_age_months", 0): continue
            if rec["sold"] < f.get("min_sold", 0): continue
            if f.get("exclude_digital", True) and rec["digital_pct"] >= 50: continue
            if f.get("exclude_custom_shops") and rec.get("accepts_custom"): continue
            # Astuce dropship: boutiques localisees en Chine/Hong Kong revendent souvent
            # des produits AliExpress => filtrer dessus AVANT Lens augmente fortement le
            # taux de validation et evite de cramer Lens sur des boutiques non-sourcables.
            if f.get("only_cn_hk") and (rec.get("country") or "").upper() not in ("CN", "HK"):
                continue
            keep_recs.append(rec)
        # 4) catalogues (titres) en PARALLELE -> niche precise
        if f.get("fetch_titles", True):
            need = [r for r in keep_recs if not r.get("titles") or len(r["titles"]) <= 1]
            budget = max_api - (listing_calls + enriched_calls)
            need = need[:max(budget, 0)]
            if need:
                with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as ex:
                    title_lists = list(ex.map(lambda r: fetch_shop_titles(r["id"]), need))
                for r, ts in zip(need, title_lists):
                    if ts:
                        r["titles"] = ts; r["sample"] = ts[0]
                        enriched_calls += 1
                        cache[r["id"]] = r
                _save(cache)
        # 5) ajout au resultat (avec FILTRE PERTINENCE: le catalogue doit vraiment
        # correspondre au mot-cle tape, pas juste 1 produit isole matche par Etsy)
        for rec in keep_recs:
            if kw_rel and keyword_relevance(shop_titles(rec), kw_rel) < rel_min:
                continue
            if kw_rel:                     # affiche un produit qui matche le mot-cle
                rec = dict(rec); rec["sample"] = match_sample(shop_titles(rec), kw_rel)
            shops.append(rec)
            last_match_credits = listing_calls + enriched_calls   # progres => on continue
            if progress: progress(len(shops), seen_listings)
            if len(shops) >= collect_target: break
        # early-abort anti-gaspillage: aucun NOUVEAU resultat depuis no_match_budget
        # credits (0 boutique au depart, OU filon epuise apres en avoir trouve quelques-unes).
        if (listing_calls + enriched_calls) - last_match_credits >= no_match_budget:
            aborted_no_match = True
            break
    # avance le curseur de pagination pour le prochain run (rotation des niches).
    # epuise ou peu de pages => on recommence du debut la prochaine fois.
    _cursor_set(ckey, 0 if exhausted else pg)
    candidates = processed
    shops.sort(key=lambda x: -x["rate"])
    # 3) raffinage IA (GLM gratuit): juge + nomme la niche + verdict dropship
    shops, ai_used = ai_enrich_shops(shops, f)
    # 4) validation dropship AliExpress (opt-in): image/produit + fallback texte
    ali_used = False
    if f.get("validate_ali") and shops:
        ali_used = True
        nprod = int(f.get("ali_products", 10) or 10)
        minm = int(f.get("ali_min_match", 3) or 3)
        enriched_calls += validate_shops_ali(shops, nprod, minm, stop=stop)
        # gate seulement si AliExpress n'a PAS bloque (sinon on garderait 0 boutique).
        # CONSENSUS Lens+IA (dropship_confirmed) pour couper les faux positifs.
        if f.get("ali_gate", True) and not any(s.get("ali_blocked") for s in shops) and not _stopped(stop):
            shops = [s for s in shops if (s.get("dropship_confirmed")
                     if s.get("dropship_confirmed") is not None else s.get("ali_validated"))]
    res = {
        "listing_calls": listing_calls,
        "shop_calls": enriched_calls,
        "ai_used": ai_used,
        "ali_used": ali_used,
        "ai_available": ai_available(),
        "api_used": listing_calls + enriched_calls,
        "listings_seen": seen_listings,
        "listings_kept": kept_listings,
        "candidates": len(candidates),
        "matched": len(shops),
        "quota_remaining": _remaining["today"],
        "aborted_no_match": aborted_no_match,
        "clusters": [],
        "shops": shops,
    }
    if aborted_no_match:
        res["notice"] = ("Arret anticipe apres %d credits (%d boutique(s) trouvee(s)): plus "
                         "de nouveau resultat pour ce mot-cle avec ces filtres. Assouplis "
                         "(baisse ventes/mois min, monte age max) ou change de mot-cle. "
                         "Credits economises." % (listing_calls + enriched_calls, len(shops)))
    # coupe a EXACTEMENT target_count + diversifie les niches
    return finalize(res, target=target_count, min_per_niche=f.get("min_per_niche", 1))

# ---------------- boutiques similaires (scan + clones) ----------------
def resolve_shop_name(text):
    """Extrait le NOM de boutique Etsy d'un lien OU d'un nom brut.
    Gere: https://www.etsy.com/shop/NOM, /fr/shop/NOM, lien produit .../shop/NOM,
    ou simplement 'Mon Shop' -> 'MonShop'."""
    import re as _re
    s = (text or "").strip()
    if not s:
        return ""
    m = _re.search(r"etsy\.[a-z.]+/(?:[a-z]{2}/)?shop/([A-Za-z0-9_-]+)", s, _re.I)
    if m:
        return m.group(1)
    m = _re.search(r"/shop/([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)
    if s.lower().startswith("http"):
        # lien sans /shop/ : on tente le dernier segment alphanumerique
        seg = [p for p in _re.split(r"[/?#]", s) if _re.fullmatch(r"[A-Za-z0-9_-]+", p)]
        if seg:
            return seg[-1]
    return _re.sub(r"\s+", "", s)   # nom de boutique Etsy = sans espaces

def lookup_shop(name):
    """Trouve une boutique Etsy par nom (API findShops) puis scanne son catalogue
    COMPLET (jusqu'a 100 titres). Retourne le record enrichi + titles, ou {error}."""
    if not name:
        return {"error": "Entre un nom ou lien de boutique."}
    try:
        d = _get("/shops?shop_name=" + urllib.parse.quote(name))
    except Exception as e:
        return {"error": "Etsy: " + str(e)[:80]}
    results = d.get("results", [])
    if not results:
        return {"error": "Boutique introuvable: " + name}
    m = None
    for r in results:                       # match exact prioritaire
        if (r.get("shop_name") or "").lower() == name.lower():
            m = r; break
    m = m or results[0]
    sid = m.get("shop_id")
    rec = _fetch_shop_record(sid)
    if rec.get("error"):
        return rec
    rec["titles"] = fetch_shop_titles(sid, 100)
    rec["sample"] = rec["titles"][0] if rec.get("titles") else ""
    return rec

def ai_shop_profile(titles, name=""):
    """L'IA scanne le catalogue COMPLET d'une boutique et en deduit un profil PRECIS:
    niche dominante + types de produits + 1-3 mots-cles ANGLAIS de recherche Etsy pour
    trouver des boutiques SIMILAIRES (meme type de produit). {} si pas de cle IA."""
    if not ai_available() or not titles:
        return {}
    items = titles[:100]
    prompt = (
        "Tu es un AGENT expert d'analyse de boutiques Etsy pour un dropshipper. On te donne "
        "la LISTE COMPLETE des titres produits d'UNE boutique" + (" (" + name + ")" if name else "") + ".\n\n"
        "Prends ton TEMPS. Lis ABSOLUMENT TOUS les titres un par un. Identifie le ou les "
        "TYPES DE PRODUITS reellement vendus et le THEME/univers de la boutique.\n\n"
        "Objectif: produire des MOTS-CLES de recherche Etsy (EN ANGLAIS) qui permettront de "
        "retrouver d'AUTRES boutiques vendant LE MEME genre de produits.\n\n"
        "Renvoie UNIQUEMENT ce JSON compact, rien d'autre:\n"
        "{\"niche\":\"<niche dominante en francais, 2-4 mots, par le PRODUIT>\","
        "\"product_types\":[\"<type produit 1 en anglais>\",\"<type 2>\"],"
        "\"keywords\":[\"<mot-cle recherche Etsy EN, 1-3 mots>\",\"<autre>\",\"<autre>\"],"
        "\"dropship\":<0..1 proba produits industriels revendables sur AliExpress>,"
        "\"summary\":\"<1 phrase: ce que vend la boutique>\"}\n"
        "REGLES keywords: 1 a 3 max, en ANGLAIS, termes que les vendeurs Etsy utilisent "
        "VRAIMENT (ex 'macrame wall hanging', 'ceramic mug', 'dog bandana'). Du plus dominant "
        "au moins dominant. PAS de marque, PAS de nom de boutique, PAS de mot generique seul "
        "('home decor', 'gift', 'handmade').\n\n"
        + json.dumps(items, ensure_ascii=False)
    )
    txt = _ai_call(prompt, max_tokens=1200)
    if not txt:
        return {}
    try:
        txt = txt[txt.find("{"): txt.rfind("}") + 1]
        p = json.loads(txt)
        p["keywords"] = [str(k).strip() for k in (p.get("keywords") or []) if str(k).strip()][:3]
        p["product_types"] = [str(k).strip() for k in (p.get("product_types") or []) if str(k).strip()][:5]
        return p
    except Exception:
        return {}

def _scan_shop_scrape(name):
    """Scan le catalogue COMPLET d'une boutique via SCRAPING (0 credit API).
    Retourne un record similaire a lookup_shop, ou {error}."""
    import scraper
    if not scraper.SCRAPLING_OK:
        return {"error": "scrapling non installe"}
    d = scraper.scrape_shop(name)
    if d.get("error") or d.get("sold") is None:
        return {"error": d.get("error") or ("boutique introuvable: " + name)}
    mo = d.get("months") or 12
    return {"id": name, "name": name, "url": "https://www.etsy.com/shop/" + name,
            "country": None, "sold": d["sold"], "months": mo,
            "rate": round(d["sold"] / mo, 1),
            "titles": d.get("titles") or [], "images": d.get("images") or [],
            "sample": (d["titles"][0] if d.get("titles") else "")}

def find_similar_shops(shop_input="", target_count=30, max_api=600, filters=None,
                       mode="live", progress=None, stop=None):
    """Colle un lien/nom de boutique -> scan complet -> trouve des boutiques SIMILAIRES
    (meme type de produits) avec produits trouvables sur AliExpress.

    mode="live"   : source + candidats via API Etsy (run_discovery). 1 credit/boutique.
    mode="scrape" : source + candidats via navigateur (run_scrape). 0 credit API.

    Pipeline:
      1. scan catalogue complet de la source (API ou scrape selon mode).
      2. ai_shop_profile                  -> niche + mots-cles EN precis.
      3. run_discovery/run_scrape par mot-cle -> candidats (filtre IA match + AliExpress).
      4. fusion/dedup, retrait de la boutique source, finalize.
    """
    f = dict(filters or {})
    # CLONE FINDER: on VALIDE AliExpress pour ETIQUETER chaque boutique (dropship vs artisan),
    # mais on NE FILTRE PLUS dur (ali_gate=False): on montre TOUTES les boutiques similaires et
    # on trie les dropship-confirmees en tete. Sinon sur une niche artisanale (ex: ustensiles
    # bois) le gate dur renvoyait 0 boutique. L'utilisateur voit tout + le statut dropship.
    f["validate_ali"] = True
    f["ali_gate"] = False                  # annoter, pas filtrer (recall preserve)
    f["use_ai"] = True                     # jugement IA dropship (consensus avec Lens)
    scrape_mode = (mode == "scrape")
    name = resolve_shop_name(shop_input)
    src = _scan_shop_scrape(name) if scrape_mode else lookup_shop(name)
    if src.get("error"):
        return {"error": src["error"], "shops": [], "clusters": []}
    titles = src.get("titles") or []
    if progress:
        progress(0, len(titles))   # signale: scan source fait
    profile = ai_shop_profile(titles, src.get("name"))
    kws = list(profile.get("keywords") or [])
    if not kws:                              # fallback sans IA: nom de niche du catalogue
        raw, _ = auto_niche(titles)
        kws = [raw.lower()]
    src_id = str(src.get("id"))
    src_nm = (src.get("name") or "").lower()
    seen = {}
    merged = []
    api_used = 0
    listing_calls = 0
    ai_used = False; ali_used = False
    # cible sur-echantillonnee par mot-cle (les filtres IA/Ali coupent ensuite)
    per_target = max(target_count, 20)
    # MODE API "INSISTANT": on ne s'arrete PAS tant que la cible (target_count) n'est pas
    # atteinte. On rejoue les mots-cles en PLUSIEURS tours; le curseur de pagination de
    # run_discovery repart a chaque tour sur de NOUVELLES pages => on ne re-scanne pas les
    # memes boutiques. On desactive l'early-abort (no_match_budget tres haut). Garde-fou:
    # max_rounds + plafond de credits pour ne pas tourner a l'infini si le filon est vide.
    f["no_match_budget"] = 10 ** 9          # desactive l'arret anticipe "plus de resultat"
    per_budget = max(200, max_api // max(len(kws), 1))
    max_rounds = int(f.get("similar_max_rounds", 0)) or (12 if not scrape_mode else 4)

    def absorb(sub):
        nonlocal api_used, listing_calls, ai_used, ali_used
        api_used += sub.get("api_used", 0)
        listing_calls += sub.get("listing_calls", 0)
        ai_used = ai_used or sub.get("ai_used", False)
        ali_used = ali_used or sub.get("ali_used", False)
        added = 0
        for s in sub.get("shops", []):
            sid = str(s.get("id"))
            if sid == src_id or (s.get("name") or "").lower() == src_nm:
                continue                    # jamais la boutique source elle-meme
            if sid in seen:
                continue
            seen[sid] = s; merged.append(s); added += 1
        return added

    rnd = 0
    while len(merged) < target_count and rnd < max_rounds and not _stopped(stop):
        rnd += 1
        round_added = 0
        for kw in kws:
            if len(merged) >= target_count or _stopped(stop):
                break
            if scrape_mode:
                sub = run_scrape(keyword=kw, target_count=per_target,
                                 filters=dict(f), progress=progress, stop=stop)
            else:
                sub = run_discovery(keyword=kw, target_count=per_target,
                                    max_api=per_budget, filters=dict(f), progress=progress, stop=stop)
            round_added += absorb(sub)
            if progress: progress(len(merged), 0)
        if round_added == 0:                # un tour entier sans NOUVELLE boutique => filon epuise
            break
    # TRI: dropship confirme d'abord (2), indetermine ensuite (1), artisan/non-dropship en
    # dernier (0); a rang egal, les meilleures ventes/mois en tete. => l'utilisateur voit les
    # vraies cibles dropship en haut, sans perdre les autres similaires.
    def _drop_rank(s):
        dc = s.get("dropship_confirmed")
        return 2 if dc is True else (0 if dc is False else 1)
    merged.sort(key=lambda x: (-_drop_rank(x), -x.get("rate", 0)))
    res = {
        "source": {
            "id": src_id, "name": src.get("name"), "url": src.get("url"),
            "country": src.get("country"), "sold": src.get("sold"),
            "rate": src.get("rate"), "months": src.get("months"),
            "catalog": len(titles), "sample_titles": titles[:8],
        },
        "profile": profile,
        "keywords": kws,
        "ai_used": ai_used,
        "ali_used": ali_used,
        "ai_available": ai_available(),
        "api_used": api_used,
        "listing_calls": listing_calls,
        "matched": len(merged),
        "quota_remaining": _remaining["today"],
        "stopped": _stopped(stop),
        "rounds": rnd,
        "clusters": [],
        "shops": merged,
    }
    return finalize(res, target=target_count, min_per_niche=f.get("min_per_niche", 1))

# ---------------- validation dropship AliExpress ----------------
def _ali_kw(title):
    import re as _re
    bad = set("personalized custom your name gift handmade set new the for with and".split())
    ws = [w for w in _re.findall(r"[a-z0-9]+", (title or "").lower()) if len(w) > 2 and w not in bad]
    return " ".join(ws[:5])

def _ali_sim(a, b):
    import re as _re
    tok = lambda s: set(w for w in _re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2)
    x, y = tok(a), tok(b)
    return len(x & y) / len(x | y) if x and y else 0.0

def _ali_text_validate(shops, nprod, min_match, sim_thresh):
    """Validation TEXTE rapide (scrapling, 0 API Etsy). Fallback si pas d'images."""
    import scraper
    for s in shops:
        titles = (s.get("titles") or [s.get("sample", "")])[:nprod]
        hits = 0; checked = 0; blocked = False; matches = []
        for t in titles:
            if not t:
                continue
            checked += 1
            items, blk = scraper.scrape_ali_search(_ali_kw(t))
            if blk:
                blocked = True
                break
            best, best_it = 0.0, ""
            for it in items:
                sim = _ali_sim(t, it)
                if sim > best:
                    best, best_it = sim, it
            if best >= sim_thresh:
                hits += 1
                matches.append({"etsy": t[:60], "ali": best_it[:60], "via": "texte", "sim": round(best, 2)})
            if hits >= min_match:
                break
        s["ali_hits"] = hits
        s["ali_via"] = {"texte": checked}
        s["ali_blocked"] = blocked
        s["ali_matches"] = matches
        s["ali_validated"] = None if blocked else (hits >= min_match)

def _smart_sample_idx(pool, k, seed=0):
    """Sampling intelligent SANS API (mode scrape): choisit k indices dans un pool de `pool`
    produits scrapes (ordre page Etsy = ranking interne deja optimise). Repartition:
      ~50% top page (premiers listings = mis en avant par Etsy / conversion+SEO),
      ~30% milieu (page 2-3 = variete / declinaisons),
      ~20% aleatoire (couverture, anti-biais).
    Deterministe par `seed` (id boutique) => reproductible. Si pool <= k: tout prendre."""
    if pool <= 0:
        return []
    if pool <= k:
        return list(range(pool))
    import random as _rnd
    rnd = _rnd.Random(seed)
    top_end = max(1, int(pool * 0.4))
    mid_end = max(top_end + 1, int(pool * 0.8))
    n_top = min(max(1, round(k * 0.5)), top_end)
    n_mid = max(1, round(k * 0.3))
    sel = list(range(n_top))                         # top page bias = premiers listings
    mid = list(range(top_end, mid_end)); rnd.shuffle(mid)
    sel += mid[:n_mid]
    rest = [i for i in range(pool) if i not in set(sel)]; rnd.shuffle(rest)
    sel += rest[:max(0, k - len(sel))]
    return sorted(set(sel))[:k]

def validate_shops_ali(shops, nprod=10, min_match=3, sim_thresh=0.30, use_image=True, stop=None):
    """Verifie si les produits d'une boutique existent A L'IDENTIQUE sur AliExpress.

    use_image=True (defaut): recupere les VRAIES images produit de la boutique (Etsy API,
    ~2 appels/boutique) et delegue a ali_image.validate_shop qui:
      - tente une recherche PAR IMAGE sur AliExpress et compare par hash perceptuel
        (average hash + distance de Hamming) => match "identique" reel, pas juste un mot-cle ;
      - retombe sur une comparaison TEXTE si l'upload image est bloque.
    Boutique validee si >= min_match produits trouves identiques. ali_blocked si captcha.
    Renvoie le nombre d'appels API Etsy consommes (images)."""
    api = 0
    # Validation UNIQUEMENT par image (Google Lens). On ne scrape PLUS AliExpress en
    # texte: ca declenche "trafic exceptionnel"/captcha Datadome. Si le navigateur Lens
    # est indispo, on n'a pas de verdict (ali_validated=None) plutot que de taper AliExpress.
    ok = False
    try:
        import ali_image
        ok = bool(getattr(ali_image, "ENGINE_OK", None)
                  if getattr(ali_image, "ENGINE_OK", None) is not None
                  else getattr(ali_image, "PATCHRIGHT_OK", False))
    except Exception:
        ok = False
    if not ok:
        for s in shops:
            s["ali_hits"] = 0; s["ali_via"] = {}; s["ali_blocked"] = False
            s["ali_matches"] = []; s["ali_validated"] = None
        return 0
    for s in shops:
        if _stopped(stop):
            # STOP demande: les boutiques non encore testees restent sans verdict (None)
            s.setdefault("ali_validated", None); s.setdefault("ali_hits", 0)
            s.setdefault("ali_blocked", False); s.setdefault("ali_matches", [])
            continue
        raw_titles = s.get("titles") or [s.get("sample", "")]
        raw_imgs = s.get("images") or []
        titles = raw_titles[:nprod]
        # Source des images produit:
        # - MODE SCRAPE: le scraper a deja recupere les images de la page boutique
        #   (s["images"], alignees sur s["titles"]) => 0 appel API Etsy.
        # - MODE API/DISCOVERY: pas d'images scrapees => on les recupere via l'API Etsy
        #   (id numerique de boutique requis). Si s["id"] n'est pas numerique (scrape),
        #   l'appel API echouerait => on ne le tente QUE si pas d'images scrapees ET id numerique.
        has_scraped = any(raw_imgs)
        # prod_imgs = liste alignee aux titres, chaque element = liste de 1-2 images du
        # MEME produit (2e image = 2e chance de match AliExpress si la 1re rate).
        if has_scraped:
            # SAMPLING INTELLIGENT (sans API): le scraper ramene jusqu'a 48 produits dans
            # l'ordre de la page Etsy (ranking interne). On echantillonne nprod indices
            # (top page + milieu + aleatoire) au lieu de prendre betement les premiers, et on
            # applique les MEMES indices aux titres ET aux images (alignement strict).
            pool = min(len(raw_titles), len(raw_imgs))
            idx = _smart_sample_idx(pool, nprod, seed=hash(str(s.get("id") or s.get("name") or "")) & 0xffffffff)
            titles = [raw_titles[i] for i in idx]
            prod_imgs = [([raw_imgs[i]] if raw_imgs[i] else []) for i in idx]
        elif str(s.get("id", "")).isdigit():
            ids = fetch_shop_listing_ids(s["id"], n=nprod)
            if ids: api += 1
            # per=3 => 3 photos/produit (memes tri score => ordre aligne aux titres). Plus
            # d'images = plus de chances que Lens relie une des vues a une URL AliExpress.
            imgs, eprices = fetch_listing_images(ids, per=3, with_prices=True) if ids else ({}, {})
            if imgs: api += 1
            prod_imgs = [imgs[lid] for lid in ids if lid in imgs]
            # prix Etsy le plus bas du lot teste => base de la marge dropship
            if eprices and s.get("price") is None:
                s["price"] = round(min(eprices.values()), 2)
        else:
            prod_imgs = []
        products = [{"title": t,
                     "image_url": (prod_imgs[i][0] if i < len(prod_imgs) and prod_imgs[i] else None),
                     "image_urls": (prod_imgs[i] if i < len(prod_imgs) else [])}
                    for i, t in enumerate(titles) if t]
        if not products:
            s["ali_hits"] = 0; s["ali_via"] = {}; s["ali_blocked"] = False
            s["ali_matches"] = []; s["ali_validated"] = None
            continue
        r = ali_image.validate_shop(products, min_match=min_match, sim_thresh=sim_thresh,
                                    test_all=True)
        s["ali_hits"] = r.get("hits", 0)
        s["ali_via"] = r.get("via", {})
        s["ali_blocked"] = bool(r.get("blocked"))
        s["ali_matches"] = r.get("matches", [])
        s["ali_validated"] = r.get("validated")
        # PRECISION: nb de matches confirmes par hash perceptuel (vignette AliExpress ~= photo
        # Etsy). Signal de confiance affiche; gating strict optionnel via ALI_VERIFY_GATE=1.
        s["ali_verified"] = sum(1 for m in r.get("matches", []) if m.get("verified"))
        # PREUVE FORTE: matches confirmes sur la page produit AliExpress (vraie og:image ~=
        # photo Etsy, pas le crop Lens). Signal le + fiable de "meme produit a la source".
        s["ali_page_confirmed"] = sum(1 for m in r.get("matches", []) if m.get("page_confirmed"))
        # SIGNAL TEXTE (dropshippers a photos custom, ex Kitchenova): produits generiques
        # trouves sur AliExpress par TEXTE quand l'image ne matche pas. Combine avec l'IA.
        s["ali_text_hits"] = r.get("text_hits", 0)
        s["ali_text_coverage"] = r.get("text_coverage", 0.0)
        # FORCE DES MATCHES (point 4): chaque produit trouve est grade exact/strong/weak avec
        # des points (70/40/15). On compte chaque categorie + le score MOYEN par produit trouve.
        # Verdict simple par boutique sur la moyenne: >=70 dropship probable, 40-70 douteux,
        # <40 probablement original (label feu tricolore pour l'UI: green/orange/red).
        _ms = r.get("matches", [])
        s["ali_exact"] = sum(1 for m in _ms if m.get("strength") == "exact")
        s["ali_strong"] = sum(1 for m in _ms if m.get("strength") == "strong")
        s["ali_weak"] = sum(1 for m in _ms if m.get("strength") == "weak")
        _pts = [m.get("points", 0) for m in _ms if m.get("points") is not None]
        avg_pts = (sum(_pts) / len(_pts)) if _pts else 0
        s["ali_match_points"] = round(avg_pts)
        if avg_pts >= 70:
            s["ali_match_verdict"] = "dropship"      # 🟢 verified image match
        elif avg_pts >= 40:
            s["ali_match_verdict"] = "doubtful"       # 🟠 possible match
        else:
            s["ali_match_verdict"] = "original"       # 🔴 no/weak match
        # VERDICT PAR PROPORTION (le vrai signal: pas "1 produit = dropship" mais la PART de
        # produits trouves sur AliExpress sur l'echantillon teste). Seuils:
        #   0 match            -> original (boutique probablement artisanale)
        #   1-2 matches        -> doute (rebrand / mix supply)
        #   coverage >= 70%    -> dropship quasi-certain
        #   3+ matches         -> dropship probable
        # checked = nb produits reellement testes (echantillon best-sellers via sort_on=score).
        _hits = r.get("hits", 0); _cov = float(r.get("coverage") or 0.0)
        if s.get("ali_blocked"):
            s["ali_supply_verdict"] = None
        elif _hits == 0:
            s["ali_supply_verdict"] = "original"
        elif _cov >= 0.70:
            s["ali_supply_verdict"] = "dropship_certain"
        elif _hits >= 3:
            s["ali_supply_verdict"] = "dropship_likely"
        else:
            s["ali_supply_verdict"] = "doubt"
        # PRIX AliExpress (cout d'achat dropshipper) recuperes via Lens.
        s["ali_price_min"] = r.get("ali_price_min")
        s["ali_price_avg"] = r.get("ali_price_avg")
        s["ali_price_med"] = r.get("ali_price_med")
        # MARGE dropship: prix de vente Etsy / prix d'achat AliExpress. Un ratio eleve
        # (Etsy >> AliExpress) = preuve forte de dropshipping. On compare au prix MEDIAN
        # AliExpress (robuste au bruit, le min seul peut etre une carte accessoire a $2).
        etsy_price = s.get("price")
        try: etsy_price = float(etsy_price) if etsy_price else None
        except Exception: etsy_price = None
        aref = s.get("ali_price_med") or s.get("ali_price_avg")
        if etsy_price and aref and aref > 0:
            s["ali_margin_ratio"] = round(etsy_price / aref, 1)
        else:
            s["ali_margin_ratio"] = None
        # CONSENSUS PRECISION: une boutique est "dropship confirme" quand DEUX signaux
        # independants concordent: (1) Lens trouve les produits sur AliExpress, ET (2) l'IA
        # juge les produits industriels-revendables (ai_dropship). Score combine 0..1 =
        # part des produits trouves (plafonnee) ponderee par la proba IA. Reduit fortement
        # les faux positifs (produit visuellement proche mais en fait artisanal unique).
        # SCORE DROPSHIP 0..100, calcule sur TOUS les produits testes (test_all=True).
        # Trois composantes ponderees:
        #   couverture (60%) = part des produits trouves identiques sur AliExpress (Lens)
        #   IA dropship (25%) = proba produits industriels-revendables (ai_dropship)
        #   marge (15%)       = prix Etsy / prix AliExpress, sature a 5x (=1.0)
        cov = float(r.get("coverage") or 0.0)           # 0..1 (n_trouves / n_testes)
        s["ali_coverage"] = round(cov, 2)
        ad = s.get("ai_dropship")
        mr = s.get("ali_margin_ratio")
        margin_norm = min(1.0, (mr - 1.0) / 4.0) if (mr and mr > 1.0) else 0.0  # 1x->0, 5x->1
        # SCORE: si l'IA est absente on n'injecte PLUS un ai_part=0.5 fictif (biais qui
        # gonflait le score meme sans preuve IA). On renormalise les poids sur les 2
        # signaux restants: coverage -> 0.80 (0.60/0.75) et marge -> 0.20 (0.15/0.75).
        # SIGNAL TEXTE (dropshippers a photos custom, ex Kitchenova): le produit generique
        # existe sur AliExpress (trouve en TEXTE) meme si l'image ne matche pas. On ne le compte
        # QUE si l'IA juge le produit industriel-revendable (ad >= 0.5) => l'IA est le garde-fou
        # qui evite de flagger un artisan dont le produit a un nom generique.
        tcov = float(s.get("ali_text_coverage") or 0.0)
        text_consensus = bool(tcov >= 0.5 and ad is not None and ad >= 0.5)
        s["dropship_suspect"] = text_consensus     # photo custom: texte + IA concordent
        text_sig = tcov if (ad is not None and ad >= 0.5) else 0.0
        if ad is None:
            score01 = (0.60 / 0.75) * cov + (0.15 / 0.75) * margin_norm
        else:
            score01 = 0.55 * cov + 0.20 * ad + 0.10 * margin_norm + 0.15 * text_sig
        if text_consensus and cov < 0.01:
            score01 = max(score01, 0.55)            # dropship "photo custom" confirme texte+IA
        s["dropship_score"] = round(score01, 2)
        s["dropship_score100"] = round(100 * score01)
        # VERDICT CONSENSUS: un seul signal (Lens) ne suffit pas a confirmer du dropship.
        # Si l'IA est absente, on exige un 2e signal independant (marge >= 3x) pour
        # confirmer; sinon le verdict reste INDETERMINE (None) plutot que d'auto-confirmer
        # (le gate de filtrage etsy_core.py:1651 retombe sur ali_validated quand
        # dropship_confirmed est None => le recall est preserve, seul le label change).
        margin_boost = bool(mr is not None and mr >= 3.0)
        # La confirmation page produit est un 2e signal independant fort (vraie image source
        # AliExpress identique a la photo Etsy) => suffit a confirmer comme la marge.
        page_boost = bool(s.get("ali_page_confirmed", 0) >= 1)
        if s.get("ali_validated") is None:
            s["dropship_confirmed"] = None
        elif s.get("ali_validated"):
            if ad is None:
                s["dropship_confirmed"] = True if (margin_boost or page_boost) else None
            else:
                s["dropship_confirmed"] = (ad >= 0.4) or margin_boost or page_boost
        else:
            # pas valide par IMAGE: confirme quand meme si consensus TEXTE + IA (photo custom).
            s["dropship_confirmed"] = True if text_consensus else False
    return api

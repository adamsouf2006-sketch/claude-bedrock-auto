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
import urllib.request, urllib.parse, urllib.error, json, time, os
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

ENRICH_WORKERS = 4      # appels API Etsy en parallele (+ retry 429 dans _get)
AI_WORKERS = 4          # lots IA en parallele (failover gere par cle)
DAY_LIMIT_DEFAULT = 5000  # quota Etsy/jour par defaut (reset 00:00 UTC)

# ---- IA optionnelle (OpenRouter). Modele GLM GRATUIT par defaut => 0 credit. ----
# Cles + modele: variables d'env OU fichier local config.local.json (gitignore).
# Plusieurs cles supportees => failover automatique (les modeles :free sont rate-limited).
def _load_ai_config():
    cfg = {"keys": [], "model": "", "anthropic": "", "etsy": ""}
    # 1) env
    env_or = os.environ.get("OPENROUTER_API_KEY", "")
    if env_or:
        cfg["keys"] = [k.strip() for k in env_or.split(",") if k.strip()]
    cfg["model"] = os.environ.get("OPENROUTER_MODEL", "")
    cfg["anthropic"] = os.environ.get("ANTHROPIC_API_KEY", "")
    cfg["etsy"] = os.environ.get("ETSY_API_KEY", "")
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
        except Exception:
            pass
    return cfg

_AICFG = _load_ai_config()
OPENROUTER_KEYS = _AICFG["keys"]
ANTHROPIC_KEY = _AICFG["anthropic"]
AI_MODEL = "claude-haiku-4-5-20251001"   # Anthropic direct (fallback)
# Modeles GRATUITS OpenRouter (0 credit). gpt-oss-120b = meilleur dispo + JSON fiable.
# Chaine de secours si rate-limit (429) sur les modeles :free.
OPENROUTER_MODEL = _AICFG["model"] or "openai/gpt-oss-120b:free"
OPENROUTER_FALLBACKS = ["openai/gpt-oss-20b:free", "meta-llama/llama-3.3-70b-instruct:free",
                        "qwen/qwen3-next-80b-a3b-instruct:free", "nvidia/nemotron-3-super-120b-a12b:free"]

def ai_available():
    return bool(OPENROUTER_KEYS or ANTHROPIC_KEY)

def ai_model_name():
    if OPENROUTER_KEYS: return OPENROUTER_MODEL
    if ANTHROPIC_KEY: return AI_MODEL
    return ""

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
    taxo = "\n".join("  - " + n for n in NICHE_TAXONOMY)
    items = [{"id": s["id"], "titres": (s.get("titles") or [s.get("sample", "")])[:40]}
             for s in chunk]
    rel_rule = ""
    rel_field = ""
    if query:
        rel_rule = (
            "\nPERTINENCE RECHERCHE: l'utilisateur cherche le PRODUIT \"" + query + "\". "
            "Comprends-le SEMANTIQUEMENT (le vrai objet voulu), pas comme une chaine de "
            "caracteres. Ex: 'support' = un socle/support/presentoir physique, PAS "
            "'emotional support'. 'bougie' = candle. Mets match=false si la boutique ne "
            "vend pas majoritairement ce produit.\n"
        )
        rel_field = "\"match\":true,"
    prompt = (
        "Tu es un AGENT autonome d'analyse de niches Etsy pour un dropshipper. Tu recois "
        "plusieurs boutiques, chacune avec la LISTE complete de ses titres produits.\n\n"
        "Prends ton TEMPS. Sois PRECIS et RIGOUREUX. N'invente rien: base-toi uniquement "
        "sur les titres fournis.\n\n"
        "METHODE OBLIGATOIRE pour CHAQUE boutique:\n"
        "1. Lis ABSOLUMENT TOUS les titres, UN PAR UN, sans en sauter. Pour chaque titre, "
        "identifie le type de produit reel (ex: 'macrame wall hanging' -> decoration murale ; "
        "'soy candle' -> bougie ; 'faux potted plant' -> plante artificielle).\n"
        "2. Compte precisement combien de titres tombent dans chaque categorie.\n"
        "3. La niche de la boutique = la categorie STRICTEMENT MAJORITAIRE (le plus de titres). "
        "Ignore les titres isoles/exceptions. Une boutique a UNE seule niche dominante.\n"
        "4. Verifie ta conclusion: relis les titres et confirme que ta niche couvre bien la "
        "majorite avant de repondre. En cas de doute entre deux niches, choisis celle qui "
        "couvre le plus de titres.\n"
        + rel_rule +
        "\nRenvoie par boutique:\n"
        "- accept (bool): true SEULEMENT si la majorite des produits sont PHYSIQUES, NON "
        "personnalises, non digitaux, sans vetement/bijou/sticker/porte-cles/electronique/"
        "gadget, pas trop lourds, sans croyance/occulte. Sinon false.\n"
        + ("- match (bool): true si la boutique vend bien le produit cherche (voir PERTINENCE). Sinon false.\n" if query else "") +
        "- niche (str): EXACTEMENT une valeur de cette liste fermee (recopie telle quelle), "
        "celle qui correspond a la MAJORITE des titres. Jamais inventer, jamais un nom de "
        "produit. Liste autorisee:\n" + taxo + "\n"
        "Si vraiment rien ne domine, mets 'Autre deco maison'.\n"
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
    chunks = [pool[i:i + batch] for i in range(0, len(pool), batch)]
    out = {}
    from functools import partial
    work = partial(_ai_refine_chunk, query=query)
    with ThreadPoolExecutor(max_workers=min(AI_WORKERS, len(chunks))) as ex:
        for d in ex.map(work, chunks):
            out.update(d)
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

# Mots trop generiques pour servir de filtre de pertinence a eux seuls.
_REL_GENERIC = {"decor", "home", "handmade", "gift", "set", "wall", "art", "deco", "custom"}

def keyword_relevance(titles, kw_en):
    """Pertinence boutique vs mot-cle (traduit EN). = part des TITRES du catalogue
    qui contiennent un mot-cle fort. Mesure la DOMINANCE: une boutique crochet avec
    1 seul article 'wood' obtient ~0.02 (rejetee), une vraie boutique bois ~0.8.
    Evite de retenir une boutique sur 1 produit isole matche par Etsy."""
    import re as _re
    toks = [w for w in _re.findall(r"[a-z]+", (kw_en or "").lower()) if len(w) > 2]
    strong = [w for w in toks if w not in _REL_GENERIC] or toks
    if not strong or not titles:
        return 1.0
    low = [t.lower() for t in titles]
    n = sum(1 for t in low if any(w in t for w in strong))
    return n / len(low)

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
    query = (f.get("_query") or "").strip()    # mot-cle traduit EN => pertinence IA
    verdict = ai_refine(shops, query=query)
    if not verdict:
        return shops, False
    thr = float(f.get("dropship_min", 0.5))
    gate_ds = bool(f.get("ai_dropship_gate"))
    kept = []
    for s in shops:
        v = verdict.get(s["id"])
        if v is None:
            kept.append(s); continue          # non juge => on garde
        if not v.get("accept", True):
            continue                            # IA rejette: hors cible
        if query and v.get("match") is False:
            continue                            # IA: boutique hors-sujet vs la recherche
        niche = snap_niche(v.get("niche"))   # force dans la taxonomie => vrai regroupement
        s["ai_niche"] = niche
        s["_niche"] = niche
        if "dropship" in v:
            try: s["ai_dropship"] = round(float(v["dropship"]), 2)
            except Exception: pass
        if v.get("reason"):
            s["ai_reason"] = v["reason"]
        if gate_ds and s.get("ai_dropship") is not None and s["ai_dropship"] < thr:
            continue                            # produit trop unique => pas dropship-able
        kept.append(s)
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

def fetch_listing_images(listing_ids, per=1):
    """1 appel batch: {listing_id: [image_urls]} pour jusqu'a 100 listings.
    includes=Images embarque les images (url_570xN)."""
    if not listing_ids:
        return {}
    try:
        b = _get("/listings/batch?listing_ids=" + ",".join(listing_ids[:100]) + "&includes=Images")
    except Exception:
        return {}
    out = {}
    for L in b.get("results", []):
        imgs = [im.get("url_570xN") or im.get("url_fullxfull") for im in (L.get("images") or [])]
        imgs = [u for u in imgs if u]
        if imgs:
            out[str(L.get("listing_id"))] = imgs[:per]
    return out

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
                r["_niche"] = snap_niche(r["ai_niche"])   # garanti dans la taxonomie
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

def run_scrape(keyword="", target_count=30, filters=None, progress=None):
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
    shops = []; seen = set(); scraped = 0; found_total = 0; skipped_pre = 0; pg = 0

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
                "titles": d["titles"] or ([sample] if sample else [])}

    empty_streak = 0
    # budget anti-boucle: au pire on charge ~15 boutiques par cible avant d'abandonner.
    max_scraped = int(f.get("max_scraped", 0)) or max(target_count * 15, 200)
    samples = {}   # name -> sample (pour le build)
    # boucle jusqu'a target_count, epuisement Etsy, ou budget atteint.
    while len(shops) < target_count and pg < 250 and scraped < max_scraped:
        pg += 1
        found = scraper.scrape_search_shops(keyword, pages=1, page_start=pg)
        if not found:
            empty_streak += 1
            if empty_streak >= 3: break   # 3 pages vides = fin du stock Etsy
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
        if not batch: continue
        # chargement PARALLELE du batch (1 navigateur, pool de pages)
        results = scraper.scrape_shops_batch(batch)
        for name in batch:
            scraped += 1
            rec = build(name, samples.get(name, ""), results.get(name, {}))
            if rec is None: continue
            cache_upsert(cache, rec)   # enregistre toute boutique trouvee, sans doublon
            if keep(rec): shops.append(rec)
            if progress: progress(len(shops), scraped)
            if len(shops) >= target_count: break
        _save(cache)
    shops.sort(key=lambda x: -x["rate"])
    res = {"source": "scrape", "api_used": 0, "scraped": scraped,
           "found": found_total, "skipped_pre": skipped_pre, "matched": len(shops),
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
def run_discovery(keyword="", target_count=100, max_api=500, filters=None, progress=None):
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
    rel_min = float(f.get("relevance_min", 0.35)) if kw_rel else 0.0
    f["_query"] = kw_rel         # pertinence IA (jugement semantique vs la recherche)
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
    while len(shops) < target_count and pg < start_pg + 120 and pg < MAX_PAGE + 5 \
          and (listing_calls + enriched_calls) < max_api:
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
            shops.append(rec)
            if progress: progress(len(shops), seen_listings)
            if len(shops) >= target_count: break
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
        enriched_calls += validate_shops_ali(shops, nprod, minm)
        # gate seulement si AliExpress n'a PAS bloque (sinon on garderait 0 boutique)
        if f.get("ali_gate", True) and not any(s.get("ali_blocked") for s in shops):
            shops = [s for s in shops if s.get("ali_validated")]
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
        "clusters": [],
        "shops": shops,
    }
    # coupe a EXACTEMENT target_count + diversifie les niches
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

def validate_shops_ali(shops, nprod=10, min_match=3, sim_thresh=0.30, use_image=True):
    """Verifie si les produits d'une boutique existent A L'IDENTIQUE sur AliExpress.

    use_image=True (defaut): recupere les VRAIES images produit de la boutique (Etsy API,
    ~2 appels/boutique) et delegue a ali_image.validate_shop qui:
      - tente une recherche PAR IMAGE sur AliExpress et compare par hash perceptuel
        (average hash + distance de Hamming) => match "identique" reel, pas juste un mot-cle ;
      - retombe sur une comparaison TEXTE si l'upload image est bloque.
    Boutique validee si >= min_match produits trouves identiques. ali_blocked si captcha.
    Renvoie le nombre d'appels API Etsy consommes (images)."""
    api = 0
    if use_image:
        try:
            import ali_image
            if not getattr(ali_image, "PATCHRIGHT_OK", False):
                use_image = False   # navigateur image indispo => texte (scrapling)
        except Exception:
            use_image = False
    if not use_image:
        _ali_text_validate(shops, nprod, min_match, sim_thresh)
        return 0
    for s in shops:
        titles = (s.get("titles") or [s.get("sample", "")])[:nprod]
        # images produit reelles -> recherche image AliExpress (match identique)
        ids = fetch_shop_listing_ids(s["id"], n=nprod)
        if ids: api += 1
        imgs = fetch_listing_images(ids, per=1) if ids else {}
        if imgs: api += 1
        img_list = [u for lst in imgs.values() for u in lst]
        products = [{"title": t, "image_url": (img_list[i] if i < len(img_list) else None)}
                    for i, t in enumerate(titles) if t]
        if not products:
            s["ali_hits"] = 0; s["ali_via"] = {}; s["ali_blocked"] = False
            s["ali_matches"] = []; s["ali_validated"] = None
            continue
        r = ali_image.validate_shop(products, min_match=min_match, sim_thresh=sim_thresh)
        s["ali_hits"] = r.get("hits", 0)
        s["ali_via"] = r.get("via", {})
        s["ali_blocked"] = bool(r.get("blocked"))
        s["ali_matches"] = r.get("matches", [])
        s["ali_validated"] = r.get("validated")
    return api

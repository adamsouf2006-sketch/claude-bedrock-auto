"""
Banc de tests COMPLET (offline, 0 quota API, 0 Chrome) des fonctions coeur de tous les modes.
But: confirmer la logique sans bruler de credits Etsy/tokens OpenRouter ni dependre du reseau.
Lancer: python tests/test_suite.py   (exit 0 = tout vert, exit 1 = au moins 1 echec)

Couvre:
  - niche_finder: dedup signature, anti-doublon entre runs, persistance, banni, reset
  - scraper: 429 Retry-After (sec + date HTTP), concurrence adaptative, throttle html
  - etsy_core: profile_drop_score (age/pays/coherence), _smart_sample_idx, _ai_sig stable
  - ali_image: defaut defensif outcome.get
  - server: routage des endpoints (HTTP reel sur port ephemere, handlers mockes)
"""
import os, sys, json, tempfile, importlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isole le cache dans un dossier temp => ne touche pas le cache reel de prod
_TMP = tempfile.mkdtemp(prefix="ns_tests_")
os.environ["ALI_CDP_AUTO"] = "0"

PASS = 0; FAIL = 0; FAILED = []

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1; FAILED.append(name)
        print(f"  FAIL {name}  {detail}")

def section(t):
    print(f"\n== {t} ==")


# ---------------------------------------------------------------- niche_finder
def test_niche_finder():
    section("niche_finder dedup + persistance")
    import etsy_core as e
    from pathlib import Path
    e.CACHE = Path(_TMP)            # redirige le cache vers temp
    import niche_finder as nf
    importlib.reload(nf)
    nf.NICHE_HIST_F = Path(_TMP) / "niche_history.json"
    nf.reset_niche_history()

    # signature: ordre + pluriel + accents + mots-outils ignores
    check("sig ordre/pluriel", nf._niche_sig("Rangement cuisine") == nf._niche_sig("cuisine rangements"))
    check("sig accents", nf._niche_sig("Décoration LED") == nf._niche_sig("decoration led"))
    check("sig mots-outils", nf._niche_sig("Accessoires de bureau") == nf._niche_sig("accessoires bureau"))
    check("sig distincte", nf._niche_sig("Bain spa") != nf._niche_sig("Cuisine inox"))

    # banni
    check("banni jardin", nf._is_banned("Deco jardin zen"))
    check("banni bijou", nf._is_banned("Bracelet acier"))
    check("non banni", not nf._is_banned("Rangement cuisine"))

    # persistance + seen_before + quasi-doublon
    nf._niche_history_add(["Rangement cuisine", "Deco LED", "Accessoires chien"])
    sigs, names = nf._niche_history_load()
    check("persist 3 noms", len(names) == 3, str(names))
    check("seen exact", nf._seen_before("Deco LED", sigs))
    check("seen reformule", nf._seen_before("cuisine rangement", sigs))
    check("seen quasi-doublon", nf._seen_before("Accessoires chiens premium", sigs))  # >=80% recouvrement
    check("non-seen nouveau", not nf._seen_before("Eclairage neon", sigs))
    # idempotence
    nf._niche_history_add(["Deco LED"])
    _, names2 = nf._niche_history_load()
    check("add idempotent", len(names2) == 3, str(names2))

    # suggest_niches E2E avec IA mockee: run2 ne doit rien repeter de run1
    nf.reset_niche_history()
    runs = [
        {"niches": [{"niche": "Rangement cuisine", "products": ["spice rack"]},
                    {"niche": "Deco LED", "products": ["led strip"]},
                    {"niche": "Accessoires chien", "products": ["dog bandana"]}]},
        {"niches": [{"niche": "Cuisine rangements", "products": ["spice jar"]},   # reformule run1
                    {"niche": "Deco LED", "products": ["led lamp"]},               # repeat exact
                    {"niche": "Salle de bain spa", "products": ["soap dish"]},     # NEW
                    {"niche": "Eclairage neon", "products": ["neon light"]}]},     # NEW
    ]
    st = {"i": 0}
    def fake_ai(prompt, max_tokens=2000):
        r = json.dumps(runs[st["i"]]); st["i"] += 1; return r
    _real_avail, _real_call = e.ai_available, e._ai_call
    e.ai_available = lambda: True
    e._ai_call = fake_ai
    try:
        r1 = [x["niche"] for x in nf.suggest_niches(10)]
        r2 = [x["niche"] for x in nf.suggest_niches(10)]
    finally:
        e.ai_available, e._ai_call = _real_avail, _real_call
    check("run1 = 3 niches", len(r1) == 3, str(r1))
    sigs1 = {nf._niche_sig(x) for x in r1}
    leaked = [x for x in r2 if nf._seen_before(x, sigs1)]
    check("run2 0 doublon", not leaked, "leaked=" + str(leaked))
    check("run2 garde le neuf", set(r2) == {"Salle de bain spa", "Eclairage neon"}, str(r2))
    nf.reset_niche_history()

    # EXHAUSTION: 1er appel IA = que des doublons connus, 2e appel (retry) = du neuf
    nf._niche_history_add(["Deco LED", "Rangement cuisine"])
    sigs_h, _ = nf._niche_history_load()
    ex_runs = [
        {"niches": [{"niche": "Deco LED", "products": ["led strip"]},          # tout deja vu
                    {"niche": "Cuisine rangements", "products": ["spice jar"]}]},
        {"niches": [{"niche": "Bain spa zen", "products": ["soap dish"]}]},     # neuf au retry
    ]
    stx = {"i": 0}
    def fake_ai2(prompt, max_tokens=2000):
        r = json.dumps(ex_runs[stx["i"]]); stx["i"] += 1; return r
    e.ai_available = lambda: True
    e._ai_call = fake_ai2
    try:
        rex = [x["niche"] for x in nf.suggest_niches(10)]
    finally:
        e.ai_available, e._ai_call = _real_avail, _real_call
    check("exhaustion retry", rex == ["Bain spa zen"], str(rex))
    check("exhaustion 2 appels", stx["i"] == 2, "appels=" + str(stx["i"]))
    nf.reset_niche_history()

    # SMOKE pipeline complet scout_niches (mode scrape, deep_top=0 => pas de Lens), tout mocke.
    nf.reset_niche_history()
    import scraper
    one_run = {"niches": [{"niche": "Deco neon", "products": ["neon sign", "led strip"]},
                          {"niche": "Rangement bureau", "products": ["pen holder"]}]}
    e.ai_available = lambda: True
    e._ai_call = lambda prompt, max_tokens=2000: json.dumps(one_run)
    e._get = lambda path: {"count": 500}                      # demande mockee
    e.ai_refine = lambda shops, **k: {}                       # pas de vrai LLM
    # echantillon scrape mocke: 2 boutiques par produit
    scraper.scrape_search_shops = lambda kw, pages=1: [("ShopA", 1), ("ShopB", 1)]
    scraper.scrape_shops_batch = lambda names: {
        n: {"titles": ["neon sign led", "rgb led strip"], "images": [], "country": "CN",
            "sold": 100, "months": 4, "rate": 8} for n in names}
    try:
        res = nf.scout_niches(filters={}, mode="scrape", n_candidates=2,
                              sample_per_niche=2, deep_top=0)
    finally:
        e.ai_available, e._ai_call = _real_avail, _real_call
    check("scout retourne niches", len(res["niches"]) == 2, str(len(res["niches"])))
    check("scout demande reelle", all(r["demand"] in (500, 1000) for r in res["niches"]),  # n_prod x 500
          str([r["demand"] for r in res["niches"]]))
    check("scout score borne 0-100", all(0 <= r["score"] <= 100 for r in res["niches"]))
    check("scout pas bloque (echantillon)", all(r["sampled"] >= 1 for r in res["niches"]))
    check("scout history rempli", res["niche_history_count"] >= 2)
    nf.reset_niche_history()

    # CAP historique: n'explose pas au-dela de 2000
    nf._niche_history_add(["niche unique numero " + str(k) for k in range(2100)])
    _, capped = nf._niche_history_load()
    check("historique borne <=2000", len(capped) <= 2000, "len=" + str(len(capped)))
    nf.reset_niche_history()

    # CACHE DEMANDE (economie credits): 1er appel = 1 credit API, 2e = cache (0 credit)
    from pathlib import Path as _P
    nf.DEMAND_CACHE_F = _P(_TMP) / "demand_test.json"
    try:
        nf.DEMAND_CACHE_F.unlink()
    except Exception:
        pass
    calls = {"n": 0}
    def fake_get(path):
        calls["n"] += 1
        return {"count": 1234}
    _rg = e._get
    e._get = fake_get
    try:
        c1 = nf._demand_product("spice rack", True)
        c2 = nf._demand_product("spice rack", True)   # doit venir du cache
        c3 = nf._demand_product("SPICE RACK", True)   # meme cle (insensible casse)
    finally:
        e._get = _rg
    check("demande valeur", c1 == 1234 and c2 == 1234 and c3 == 1234)
    check("demande cache 1 appel", calls["n"] == 1, "appels=" + str(calls["n"]))
    # expiration => re-interroge
    nf._DEMAND_TTL = -1
    e._get = fake_get
    try:
        nf._demand_product("spice rack", True)
    finally:
        e._get = _rg
    check("demande TTL expire re-appel", calls["n"] == 2, "appels=" + str(calls["n"]))
    nf._DEMAND_TTL = 24 * 3600.0


# ---------------------------------------------------------------- scraper 429
def test_scraper_429():
    section("scraper anti-429")
    import scraper
    class R:  # reponse avec Retry-After en secondes
        headers = {"retry-after": "12"}
    check("retry-after secondes", scraper._retry_after_secs(R()) == 12.0)
    class R0:
        headers = {}
    check("retry-after absent", scraper._retry_after_secs(R0()) == 0.0)
    # format date HTTP futur => > 0
    from email.utils import format_datetime
    from datetime import datetime, timezone, timedelta
    class RD:
        headers = {"Retry-After": format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30))}
    ra = scraper._retry_after_secs(RD())
    check("retry-after date HTTP", 20 <= ra <= 31, "ra=" + str(ra))
    # cap anti-gel: valeur enorme bornee a CDP_RA_MAX
    class RBig:
        headers = {"retry-after": "999999"}
    check("retry-after cap", scraper._retry_after_secs(RBig()) == scraper.CDP_RA_MAX,
          str(scraper._retry_after_secs(RBig())))
    class RBigDate:
        headers = {"Retry-After": format_datetime(datetime.now(timezone.utc) + timedelta(hours=5))}
    check("retry-after date cap", scraper._retry_after_secs(RBigDate()) == scraper.CDP_RA_MAX)

    # throttle html detect
    check("throttle 429", scraper._is_throttled_html(429, "<html>ok</html>"))
    check("throttle datadome", scraper._is_throttled_html(200, "<html>datadome captcha</html>"))
    check("non-throttle 200", not scraper._is_throttled_html(200, "<html>" + "x" * 6000 + "</html>"))

    # concurrence adaptative
    scraper._cdp_throttle["cool_until"] = 0.0
    check("conc nominale", scraper._eff_conc() == scraper.CDP_CONC)
    scraper._note_throttle(5)
    check("conc=1 post-429", scraper._eff_conc() == 1)
    import time
    check("pause globale posee", scraper._cdp_throttle["until"] > time.time())
    scraper._cdp_throttle["cool_until"] = 0.0
    check("conc recover", scraper._eff_conc() == scraper.CDP_CONC)


# ---------------------------------------------------------------- etsy_core
def test_etsy_core():
    section("etsy_core scoring + sampling")
    import etsy_core as e
    # profile_drop_score: pays CN domine
    check("drop CN domine", e.profile_drop_score({"ai_dropship": 0.2, "country": "CN", "months": 60}) >= 0.90)
    # jeune + generique => haut
    s_young = e.profile_drop_score({"ai_dropship": 0.6, "country": "US", "months": 3})
    check("jeune presomption", s_young >= 0.70, str(s_young))
    # vieux coherent => bas
    s_old = e.profile_drop_score({"ai_dropship": 0.6, "country": "US", "months": 60,
                                  "titles": ["ceramic mug"] * 8})
    check("vieux plafonne", s_old <= 0.20, str(s_old))
    # pas de jugement IA => None
    check("pas IA => None", e.profile_drop_score({"country": "US"}) is None)

    # _smart_sample_idx: deterministe, borne, dans le pool
    idx1 = e._smart_sample_idx(50, 10, seed=123)
    idx2 = e._smart_sample_idx(50, 10, seed=123)
    check("sample deterministe", idx1 == idx2, str(idx1))
    check("sample taille<=k", len(idx1) <= 10)
    check("sample dans pool", all(0 <= i < 50 for i in idx1))
    check("sample uniques", len(set(idx1)) == len(idx1))
    check("sample pool<=k => tout", e._smart_sample_idx(5, 10) == [0, 1, 2, 3, 4])
    check("sample pool 0", e._smart_sample_idx(0, 10) == [])

    # _ai_sig stable selon (query, titres)
    sh = {"id": "X", "titles": ["a", "b"]}
    check("ai_sig stable", e._ai_sig("rug", sh) == e._ai_sig("rug", dict(sh)))
    check("ai_sig change query", e._ai_sig("rug", sh) != e._ai_sig("vase", sh))

    # keyword_relevance: dominance catalogue
    check("relevance tout", e.keyword_relevance(["led strip", "led lamp"], "led") == 1.0)
    check("relevance rien", e.keyword_relevance(["wooden bowl", "ceramic mug"], "led") == 0.0)
    check("relevance moitie", e.keyword_relevance(["led strip", "wooden bowl"], "led") == 0.5)
    check("relevance kw vide", e.keyword_relevance(["x", "y"], "") == 1.0)
    check("relevance titres vides", e.keyword_relevance([], "led") == 1.0)

    # catalog_coherence: artisan focalise vs revendeur eclate
    coh_art, sim_art = e.catalog_coherence(["olive wood spoon", "olive wood bowl",
                                            "olive wood board", "olive wood fork"])
    check("coherence artisan", coh_art is True, str((coh_art, sim_art)))
    coh_rev, _ = e.catalog_coherence(["led strip rgb", "garden hose reel",
                                      "ceramic dog bowl", "phone car mount"])
    check("coherence revendeur", coh_rev is False, str(coh_rev))
    check("coherence trop peu", e.catalog_coherence(["a b c", "d e f"]) == (None, None))

    # match_sample: titre contenant le mot-cle, sinon 1er
    check("match_sample trouve", e.match_sample(["wooden bowl", "led desk lamp"], "led") == "led desk lamp")
    check("match_sample fallback", e.match_sample(["wooden bowl", "ceramic mug"], "led") == "wooden bowl")
    check("match_sample vide", e.match_sample([], "led") == "")

    # catalog_reject: rejet PROPORTIONNEL (boutique rejetee seulement si mauvaise cat domine)
    perso = ["personalized name necklace custom", "custom monogram gift personalized",
             "name engraved custom personalized", "personalized photo custom gift"]
    digi = ["printable wall art svg", "digital download printable",
            "svg cut file digital", "printable planner pdf"]
    clean = ["olive wood bowl", "olive wood spoon", "wooden cutting board", "wood serving tray"]
    check("reject perso domine", e.catalog_reject(perso, {"exclude_perso": True}) == (True, "perso"))
    check("reject perso off", e.catalog_reject(perso, {"exclude_perso": False}) == (False, ""))
    check("reject digital domine", e.catalog_reject(digi, {})[0] is True)
    check("reject clean garde", e.catalog_reject(clean, {}) == (False, ""))
    check("reject vide", e.catalog_reject([], {}) == (False, ""))

    # _ratio_ge (early-exit) STRICTEMENT identique a (_ratio >= seuil) sur cas varies (req 0 erreur)
    import random as _r
    _r.seed(7)
    words = ["printable", "svg", "wooden", "bowl", "custom", "name", "led", "digital download"]
    ge_ok = True
    for _ in range(300):
        ti = [" ".join(_r.choice(words) for _ in range(_r.randint(1, 6)))
              for _ in range(_r.randint(1, 12))]
        for kw, th in ((e.DIGITAL_TITLE_KW, e.DIGITAL_REJECT), (e.PERSO_TITLE_KW, e.PERSO_REJECT)):
            if e._ratio_ge(ti, kw, th) != (e._ratio(ti, kw) >= th):
                ge_ok = False
    check("ratio_ge == ratio>=seuil", ge_ok)
    check("ratio_ge vide", e._ratio_ge([], e.DIGITAL_TITLE_KW, 0.5) is False)

    # resolve_keyword: vide, mot deja EN, traduction mot-a-mot FR->EN
    check("resolve vide", e.resolve_keyword("") == ("", False))
    kw, tr = e.resolve_keyword("ceramic mug")
    check("resolve EN intact", "ceramic" in kw and "mug" in kw)

    # search_cache: ne crashe PAS sur enregistrement cache incomplet (rate/months absents) et
    # respecte min_sold. On mocke _load => 0 quota/IO. use_ai False => pas d'appel IA.
    # cas REEL: month/country peuvent etre None (age inconnu). Ne doit ni crasher ni jeter a tort.
    recs = {
        "good": {"id": "good", "name": "good", "sold": 100, "rate": 5,
                 "months": 4, "titles": ["led strip light", "neon sign"]},
        "ageless": {"id": "age", "name": "age", "sold": 50, "rate": 3,
                    "months": None, "titles": ["led lamp"]},          # age inconnu (cas reel)
        "lowsold": {"id": "low", "name": "low", "sold": 1, "rate": 2,
                    "months": 10, "titles": ["led bulb"]},
    }
    _real_load = e._load
    from pathlib import Path as _P
    _real_shown = e.SHOWN_F
    e._load = lambda: recs
    e.SHOWN_F = _P(_TMP) / "shown_test.json"     # registre 'deja vu' isole => 0 pollution prod
    try:
        res = e.search_cache(filters={"use_ai": False, "min_sold": 10}, keyword="")
    finally:
        e._load = _real_load
        e.SHOWN_F = _real_shown
    ids = {s.get("id") for s in res["shops"]}
    check("cache age None pas jete", "age" in ids, str(ids))
    check("cache min_sold filtre", "low" not in ids, str(ids))


# ---------------------------------------------------------------- ali_image
def test_ali_image():
    section("ali_image robustesse")
    import ali_image as ai
    # le fix .get : un id absent ne doit pas crasher l'agregation
    outcome = {}  # simule _drive coupe avant tout test
    prod = {"title": "x"}
    hit, via, detail = outcome.get(id(prod), (False, "skipped", {}))
    check("outcome.get defaut", (hit, via) == (False, "skipped"))

    # ---- fonctions pures de matching (cle de la detection drop, req 7) ----
    # similarite titre (Jaccard tokens)
    check("sim identique", ai._sim("led strip light", "led strip light") == 1.0)
    check("sim disjoint", ai._sim("ceramic mug", "dog leash") == 0.0)
    check("sim vide", ai._sim("", "x") == 0.0)
    check("sim partiel", 0 < ai._sim("led strip light", "led wall light") < 1)

    # prix: formats varies -> float ; junk -> None
    check("prix $", ai._parse_price("$5.99") == 5.99)
    check("prix US $", ai._parse_price("US $12.34") == 12.34)
    check("prix virgule euro", ai._parse_price("8,50 €") == 8.50)
    check("prix £", ai._parse_price("£3.20") == 3.20)
    check("prix junk", ai._parse_price("livraison gratuite") is None)
    check("prix vide", ai._parse_price("") is None)

    # Hamming + hash_dist (MAX des 2 hash) + compat tuple
    check("ham simple", ai._ham(0b1010, 0b0011) == 2)
    check("ham None=64", ai._ham(None, 5) == 64)
    check("hash_dist MAX", ai._hash_dist((0b1010, 0b0000), (0b0011, 0b0000)) == 2)
    check("hash_dist absent=64", ai._hash_dist(None, (1, 2)) == 64)
    check("hamming dispatch tuple", ai._hamming((1, 1), (1, 1)) == 0)
    check("hamming brut int", ai._hamming(0b11, 0b00) == 2)

    # grade: exact (verifie + tres proche) / strong (proche) / weak (loin ou non verifie)
    check("grade exact", ai._grade(0.9, True, 4) == ("exact", ai._POINTS["exact"]))
    check("grade strong", ai._grade(0.9, False, 20) == ("strong", ai._POINTS["strong"]))
    check("grade weak loin", ai._grade(0.9, True, 40) == ("weak", ai._POINTS["weak"]))
    check("grade weak non verif", ai._grade(0.9, False, None) == ("weak", ai._POINTS["weak"]))

    # dedup produits quasi-identiques (anti sur-estimation drop)
    reps, n = ai._dedup_unique([{"txt": "led strip light rgb"},
                                {"txt": "led strip light rgb color"},   # ~doublon
                                {"txt": "wooden cutting board"}])
    check("dedup compte uniques", n == 2, "n=" + str(n))

    # precision_gate: strong borderline NON corrobore => degrade en weak
    d = {"strength": "strong", "hash_dist": ai._HASH_STRONG_SAFE + 5, "sim": 0.0,
         "page_confirmed": 0, "points": ai._POINTS["strong"]}
    ai._precision_gate(d)
    check("gate degrade borderline", d["strength"] == "weak" and d.get("gated") is True)
    # strong corrobore par titre => garde strong
    d2 = {"strength": "strong", "hash_dist": ai._HASH_STRONG_SAFE + 5,
          "sim": ai._SIM_CORROB + 0.1, "page_confirmed": 0}
    ai._precision_gate(d2)
    check("gate garde corrobore", d2["strength"] == "strong")

    # _build_detail total: results vide => detail 'none' sans IndexError
    bd = ai._build_detail("x", [], "aliexpress")
    check("build_detail vide sur", bd["strength"] == "none" and bd["n"] == 0)
    bd2 = ai._build_detail("led strip", [{"txt": "led strip light", "price": "$5.99",
                                          "url": "https://aliexpress.com/item/1.html"}], "aliexpress")
    check("build_detail non vide", bd2["n"] == 1 and bd2["sim"] > 0)


# ---------------------------------------------------------------- similar (lien boutique)
def test_similar():
    section("mode similar: parse lien boutique (req 6)")
    import etsy_core as e
    r = e.resolve_shop_name
    check("url shop", r("https://www.etsy.com/shop/MyShop") == "MyShop")
    check("url locale /fr/", r("https://www.etsy.com/fr/shop/MyShop") == "MyShop")
    check("url query", r("https://www.etsy.com/shop/MyShop?ref=hdr") == "MyShop")
    check("url path suite", r("https://www.etsy.com/shop/MyShop/items?x=1") == "MyShop")
    check("nom brut espaces", r("My Shop") == "MyShop")
    check("nom simple", r("CoolStore") == "CoolStore")
    check("vide", r("") == "")
    check("domaine majuscule", r("HTTPS://WWW.ETSY.COM/SHOP/AbcDef") == "AbcDef")
    # input vide => find_similar_shops rend une erreur propre (0 reseau)
    res = e.lookup_shop("")
    check("lookup vide => erreur", bool(res.get("error")))


# ---------------------------------------------------------------- agents (verdict drop)
def test_agents():
    section("agents referee (hierarchie preuves drop)")
    import agents
    # 1) preuve FORTE (page AliExpress confirmee) => confirme, haute confiance
    s = {"ali_page_confirmed": 1, "country": "US", "ai_profile_drop": 0.3}
    conf, ok = agents.referee(s)
    check("strong => confirme", ok and conf >= 0.85, str((conf, ok)))
    check("strong pose verdict", s["final_verdict"] == "keep" and s["dropship_confidence"] >= 85)

    # 2) pays usine CN sans image => confirme (usine deguisee)
    s = {"country": "CN", "ai_profile_drop": 0.2}
    conf, ok = agents.referee(s)
    check("CN => confirme", ok and conf >= 0.85, str((conf, ok)))

    # 3) >=2 produits vision 'meme' MAIS photos artisan reelles + pas de page Ali + pas CN
    #    => VETO photos reelles, non confirme
    s = {"ali_detail_same": 2, "ai_photo_drop": 0.1, "country": "US", "ai_profile_drop": 0.3}
    conf, ok = agents.referee(s)
    check("veto photos reelles", (not ok) and conf <= 0.45, str((conf, ok)))

    # 4) profil seul, aucune preuve image => presomption plafonnee, non confirme
    s = {"country": "US", "ai_profile_drop": 0.6}
    conf, ok = agents.referee(s)
    check("profil seul plafonne", (not ok) and conf <= 0.45, str((conf, ok)))

    # 4b) CAS REEL (nestgestaltung): VIEIL artisan (94 mois) avec IA brute HAUTE (0.7) et SANS
    #     preuve image => l'age plafonne le profil a 0.20 => referee NE confirme PAS (0% drop).
    #     Verrouille le faux positif signale par l'utilisateur.
    import etsy_core as _e2
    old_art = {"name": "nestgestaltung", "country": "DE", "months": 94, "ai_dropship": 0.7,
               "titles": ["spice rack wood", "wall organizer", "kitchen shelf"]}
    prof = _e2.profile_drop_score(old_art)
    check("vieux artisan profil bas", prof is not None and prof <= 0.20, "prof=" + str(prof))
    old_art["ai_profile_drop"] = prof
    conf_oa, ok_oa = agents.referee(old_art)
    check("vieux artisan non confirme", (not ok_oa) and conf_oa < 0.55, str((conf_oa, ok_oa)))

    # GATE ai_dropship_gate: doit filtrer sur le PROFIL age-aware, pas l'IA brute. Un vieil
    # artisan (IA brute 0.7 mais profil 0.2) doit etre EXCLU par le gate (seuil 0.5).
    _av_g = _e2.ai_available
    _ar_g = _e2.ai_refine
    _e2.ai_available = lambda: True
    _e2.ai_refine = lambda shops, **k: {s["id"]: {"accept": True, "match": True, "niche": "Cuisine",
                                                  "dropship": 0.7} for s in shops}
    try:
        young = {"id": "y", "name": "y", "country": "CN", "months": 4,
                 "titles": ["led strip", "rgb light"]}              # jeune+CN => profil haut
        old = {"id": "o", "name": "o", "country": "DE", "months": 94,
               "titles": ["spice rack wood", "kitchen shelf"]}      # vieux => profil plafonne 0.2
        kept, _u = _e2.ai_enrich_shops([young, old],
                                       {"use_ai": True, "ai_dropship_gate": True,
                                        "dropship_min": 0.5, "_query": "cuisine"})
    finally:
        _e2.ai_available = _av_g
        _e2.ai_refine = _ar_g
    kept_ids = {s["id"] for s in kept}
    check("gate garde jeune drop", "y" in kept_ids, str(kept_ids))
    check("gate exclut vieux artisan", "o" not in kept_ids, str(kept_ids))

    # 5) vision CONTREDIT (plus de 'differents' que 'meme') => confiance rabaissee
    s = {"ali_detail_same": 1, "ali_detail_diff": 3, "ali_validated": True,
         "country": "US", "ai_profile_drop": 0.3}
    conf, ok = agents.referee(s)
    check("vision contredit baisse", conf <= 0.40, str((conf, ok)))

    # 6) agent_scout sans IA => laisse tout passer + marque transparence
    import etsy_core as e
    _av = e.ai_available
    e.ai_available = lambda: False
    try:
        shops = [{"id": "a", "titles": ["x"]}, {"id": "b", "titles": ["y"]}]
        surv, used = agents.agent_scout(shops, {"use_ai": True})
    finally:
        e.ai_available = _av
    check("scout IA off passe tout", len(surv) == 2 and used is False)
    check("scout marque transparence", "IA off" in shops[0]["team"]["scout"])

    # ---- COACH orchestrate (funnel + tri + gates), 0 reseau ----
    _av2 = e.ai_available
    e.ai_available = lambda: False
    try:
        sh = [
            {"id": "cn", "name": "cn", "rate": 10, "country": "CN", "ai_profile_drop": None,
             "ai_dropship": 0.2, "titles": ["x"], "months": 3},
            {"id": "us", "name": "us", "rate": 50, "country": "US", "ai_profile_drop": 0.1,
             "ai_dropship": 0.1, "titles": ["y"], "months": 60},
        ]
        r = agents.orchestrate(sh, {"use_ai": False, "validate_ali": False, "ali_gate": False})
        check("orch funnel recues", r["funnel"]["scrapees_ou_recues"] == 2)
        check("orch tri confiance", [s["id"] for s in r["shops"]] == ["cn", "us"],
              str([s["id"] for s in r["shops"]]))   # CN haute confiance avant US gros rate
        check("orch pas gate sans image", len(r["shops"]) == 2 and r["note"] == "")

        # gate strict demande MAIS image n'a pas tourne (validate_ali False) => ne vide pas
        r2 = agents.orchestrate([dict(x) for x in sh],
                                {"use_ai": False, "validate_ali": False, "ali_gate": True})
        check("orch gate inactif si pas d'image", len(r2["shops"]) == 2)

        # filet JAMAIS VIDE: image a tourne (mock no-op) mais 0 drop confirme => below_threshold + note
        _val = e.validate_shops_ali
        _vis = e.vision_available
        e.validate_shops_ali = lambda shops, *a, **k: 0
        e.vision_available = lambda: False
        try:
            sh3 = [{"id": "u1", "name": "u1", "rate": 5, "country": "US",
                    "ai_profile_drop": 0.1, "ai_dropship": 0.1, "titles": ["mug"], "months": 50}]
            r3 = agents.orchestrate(sh3, {"use_ai": False, "validate_ali": True, "ali_gate": True})
        finally:
            e.validate_shops_ali = _val
            e.vision_available = _vis
        check("orch filet pas vide", len(r3["shops"]) == 1)
        check("orch filet below_threshold", r3["shops"][0].get("below_threshold") is True)
        check("orch filet note", bool(r3["note"]))
    finally:
        e.ai_available = _av2


# ---------------------------------------------------------------- server routing
def test_server_routing():
    section("server routage endpoints")
    import server, scraper, etsy_core as e
    from pathlib import Path as _P
    # mock les sondes reseau/Chrome => on teste le ROUTAGE, pas la connectivite Etsy live
    scraper.etsy_session_ok = lambda: True
    scraper.SCRAPE_VIA_CHROME = False
    # mode discover source=cache: base locale mockee + registre 'deja vu' isole (0 reseau/quota)
    e._load = lambda: {
        "s1": {"id": "s1", "name": "s1", "sold": 80, "rate": 4, "months": 6,
               "titles": ["led strip light"]},
    }
    e.SHOWN_F = _P(_TMP) / "shown_srv.json"
    try:
        e.SHOWN_F.unlink()
    except Exception:
        pass
    from http.server import ThreadingHTTPServer
    import threading, urllib.request
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def g(p):
        return urllib.request.urlopen(f"http://127.0.0.1:{port}{p}", timeout=10).read().decode()
    try:
        check("quota json", "remaining_today" in g("/api/quota"))
        check("niche reset", json.loads(g("/api/niche_finder_reset")).get("reset") is True)
        check("etsy_status", "session_ok" in g("/api/etsy_status"))
        # mode discover source=cache bout-en-bout (route -> search_cache -> finalize)
        dres = json.loads(g("/api/discover?source=cache&use_ai=false&min_sold=10"))
        check("discover cache source", dres.get("source") == "cache")
        check("discover cache rend boutique", any(s.get("id") == "s1" for s in dres.get("shops", [])),
              str([s.get("id") for s in dres.get("shops", [])]))
        # 404 connu
        try:
            g("/api/nope")
            check("404 inconnu", False)
        except urllib.error.HTTPError as ex:
            check("404 inconnu", ex.code == 404)
    finally:
        srv.shutdown()


def main():
    for fn in (test_niche_finder, test_scraper_429, test_etsy_core,
               test_ali_image, test_similar, test_agents, test_server_routing):
        try:
            fn()
        except Exception as ex:
            global FAIL
            FAIL += 1; FAILED.append(fn.__name__ + " (EXCEPTION)")
            import traceback
            print(f"  EXC  {fn.__name__}: {ex}")
            traceback.print_exc()
    print(f"\n==== {PASS} ok / {FAIL} fail ====")
    if FAILED:
        print("ECHECS:", ", ".join(FAILED))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

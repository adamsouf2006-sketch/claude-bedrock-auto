"""
niche_finder.py — NICHE FINDER: "je n'ai pas d'idee de niche, trouve-moi des niches OU il y a
beaucoup de dropshippers AliExpress ET beaucoup de demande/acheteurs".

Probleme resolu: se lancer a l'aveugle dans une niche (ex: cuisine) ou il y a peu de drop fait
perdre du temps. Cet outil ANALYSE plusieurs niches candidates et les CLASSE par potentiel drop.

NICHE != PRODUIT (distinction CRITIQUE). Une NICHE = une FAMILLE/categorie qui contient PLUSIEURS
produits differents (ex niche "Rangement cuisine" = {egouttoir, porte-ustensiles, range-epices,
distributeur...}). Un PRODUIT = un seul article (ex "egouttoir bambou"). On evalue la niche en
AGREGEANT sur PLUSIEURS de ses produits (demande sommee, drop echantillonne sur tous), JAMAIS sur
un seul mot-cle produit (sinon on juge un produit, pas la niche).

Methode en 2 PHASES (compromis couverture/preuve, choix utilisateur):
  PHASE 1 (rapide, BEAUCOUP de niches): pour chaque niche, on echantillonne des boutiques sur
    PLUSIEURS de ses produits (scrape OU api), on juge le drop par PROFIL+IA+pays (PAS de Lens =>
    secondes/niche) et on mesure la DEMANDE (somme des annonces actives Etsy sur ses produits). On classe.
  PHASE 2 (profonde, TOP niches seulement): on relance avec la validation IMAGE (Lens+vision)
    => PREUVE reelle de drop + exemples de boutiques.

Les niches candidates (nom de FAMILLE + sa LISTE de produits) sont GENEREES par l'IA a chaque run
(analyse ouverte, pas figee), en excluant les niches deja faites (jardin/plantes, bijoux, perso,
digital). Reutilise les pipelines eprouves d'etsy_core (run_scrape / run_discovery => orchestrate).
"""
import json, os
import etsy_core as e

# Niches DEJA faites / a EXCLURE (regle utilisateur + memoire projet).
BANNED = {"jardin", "garden", "plante", "plant", "fleur", "flower", "bijou", "jewelry",
          "jewellery", "necklace", "bracelet", "earring", "ring", "collier",
          "personnalis", "personalized", "personalised", "custom name", "monogram",
          "digital", "printable", "svg", "clipart", "vintage"}

DROP_PROFILE_MIN = float(os.environ.get("NF_DROP_PROFILE_MIN", "0.6"))
# Nb de PRODUITS distincts echantillonnes par niche pour la juger (pas 1 => sinon on juge le
# produit, pas la niche). Reglable via NF_PRODUCTS_PER_NICHE.
PRODUCTS_PER_NICHE = int(os.environ.get("NF_PRODUCTS_PER_NICHE", "3"))
# Nb MAX de boutiques passees a la preuve image (Lens) par niche en phase 2. Lens ~25s/boutique;
# le budget temps interne (ALI_TIME_BUDGET) doit suffire => on cape pour qu'il teste VRAIMENT
# chacune (avant: 15 boutiques, budget epuise apres 2 => 0 confirme a tort). Reglable NF_PHASE2_MAX.
PHASE2_MAX_SHOPS = int(os.environ.get("NF_PHASE2_MAX", "5"))


def _is_banned(name):
    low = (name or "").lower()
    return any(b in low for b in BANNED)


# ---------------- registre niches DEJA PROPOSEES (anti-doublon entre runs) ----------------
# Toute niche deja proposee par l'IA (sur n'importe quel run) est memorisee ici. Aux runs
# suivants on l'EXCLUT de la generation (prompt IA) ET on la filtre du resultat => jamais 2x
# la meme niche. Matching par "signature" de tokens (ordre/pluriel/accents ignores) pour
# attraper les quasi-doublons ("Rangement cuisine" == "cuisine rangements"). Vidable en
# supprimant cache/niche_history.json (ou via reset_niche_history()).
NICHE_HIST_F = e.CACHE / "niche_history.json"

_STOP_TOK = {"de", "des", "du", "la", "le", "les", "et", "a", "au", "aux", "pour",
             "en", "the", "of", "and", "for", "to", "with"}


def _norm_tokens(name):
    """Tokens normalises d'un nom de niche: minuscule, sans accents, sans pluriel simple,
    sans mots-outils. Sert de signature stable pour comparer 2 niches."""
    import unicodedata, re
    s = unicodedata.normalize("NFKD", (name or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    toks = re.findall(r"[a-z0-9]+", s)
    out = set()
    for t in toks:
        if t in _STOP_TOK or len(t) <= 1:
            continue
        if len(t) > 3 and t.endswith("s"):   # pluriel simple
            t = t[:-1]
        out.add(t)
    return out


def _niche_sig(name):
    return frozenset(_norm_tokens(name))


def _niche_history_load():
    """Retourne (set de signatures, liste des noms bruts deja proposes)."""
    try:
        d = json.loads(NICHE_HIST_F.read_text(encoding="utf-8"))
        names = list(d.get("names") or [])
    except Exception:
        names = []
    sigs = {_niche_sig(n) for n in names if n}
    return sigs, names


def _seen_before(name, sigs):
    """True si une niche equivalente (meme signature, ou tres fort recouvrement) est deja vue."""
    sig = _niche_sig(name)
    if not sig:
        return False
    if sig in sigs:
        return True
    for s in sigs:
        if not s:
            continue
        inter = len(sig & s)
        # quasi-doublon: recouvrement >= 80% du plus petit ensemble (et au moins 2 tokens communs)
        if inter >= 2 and inter >= 0.8 * min(len(sig), len(s)):
            return True
    return False


def _niche_history_add(names):
    """Ajoute des noms de niche au registre (apres proposition). Atomique."""
    _sigs, existing = _niche_history_load()
    merged = list(existing)
    seen = {_niche_sig(n) for n in existing}
    changed = False
    for nm in names or []:
        sig = _niche_sig(nm)
        if not sig or sig in seen:
            continue
        seen.add(sig); merged.append(nm); changed = True
    if not changed:
        return
    # borne la taille (garde les 2000 derniers noms) pour ne pas grossir sans fin
    if len(merged) > 2000:
        merged = merged[-2000:]
    tmp = NICHE_HIST_F.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps({"names": merged}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(NICHE_HIST_F)
    except Exception:
        pass


def reset_niche_history():
    """Oublie toutes les niches deja proposees (autorise a les re-proposer)."""
    try: NICHE_HIST_F.unlink()
    except Exception: pass


def suggest_niches(n=15):
    """Genere n NICHES candidates via l'IA. Chaque niche = {niche, products:[...]} ou:
      - niche   = nom de FAMILLE/categorie (2-4 mots) regroupant plusieurs produits.
      - products= 3-5 mots-cles de recherche Etsy EN ANGLAIS, des produits DIFFERENTS de la niche
                  (pas des synonymes du meme produit).
    Repli statique: inverse demand_scan.PRODUCTS (produit->niche) en niche->[produits]."""
    out = []
    hist_sigs, hist_names = _niche_history_load()
    # extrait visible pour l'IA: les derniers noms deja proposes (cap pour ne pas exploser le prompt)
    excl_txt = ", ".join(hist_names[-120:]) if hist_names else ""
    def _gen_ai(exclude_txt):
        """1 appel IA -> liste [{niche, products}]. exclude_txt = noms a NE PAS reproposer."""
        prompt = (
            "Tu es expert du dropshipping sur Etsy. Propose " + str(n) + " NICHES de produits "
            "PHYSIQUES ou un dropshipper peut sourcer sur AliExpress/Alibaba et revendre sur Etsy.\n"
            "DISTINCTION CRITIQUE niche vs produit: une NICHE est une FAMILLE/categorie qui contient "
            "PLUSIEURS produits DIFFERENTS (ex: niche 'Rangement cuisine' contient egouttoir, "
            "porte-ustensiles, range-epices, distributeur d'oeufs...). Un PRODUIT est un seul article. "
            "NE propose PAS un produit deguise en niche.\n"
            "Cible des niches a la fois (1) avec BEAUCOUP de demande/acheteurs sur Etsy et (2) ou "
            "l'on trouve DEJA beaucoup de revendeurs/dropshippers (produits industriels: gadgets, "
            "deco usine, led, resine, silicone, inox, plastique...).\n"
            "INTERDIT (ne propose PAS): jardin/plantes, bijoux, personnalise/monogramme, digital/"
            "printable, vintage.\n"
            + ("DEJA PROPOSEES AUX RUNS PRECEDENTS (ne les repropose SOUS AUCUN PRETEXTE, ni "
               "synonymes, ni reformulations): " + exclude_txt + ".\n" if exclude_txt else "")
            + "Propose des niches NOUVELLES, differentes de cette liste.\n"
            "Pour CHAQUE niche donne:\n"
            "- niche: nom de la FAMILLE en francais (2-4 mots, une categorie de produit).\n"
            "- products: 3 a 5 mots-cles de recherche Etsy EN ANGLAIS, chacun un produit DIFFERENT "
            "de cette niche (pas des synonymes), 1-3 mots, ce que les acheteurs tapent vraiment.\n"
            "Varie les univers (deco, cuisine, bureau, fete, bain, animaux, tech-accessoires, "
            "rangement, luminaire...). Pas de doublon de niche.\n"
            "Reponds UNIQUEMENT en JSON: {\"niches\":[{\"niche\":\"..\",\"products\":[\"..\",\"..\",\"..\"]}]}"
        )
        txt = e._ai_call(prompt, max_tokens=1600)
        res = []
        if txt:
            try:
                txt = txt[txt.find("{"): txt.rfind("}") + 1]
                for x in json.loads(txt).get("niches", []):
                    nm = str(x.get("niche") or "").strip()
                    prods = [str(p).strip() for p in (x.get("products") or []) if str(p).strip()]
                    prods = [p for p in prods if not _is_banned(p)]
                    if nm and prods and not _is_banned(nm):
                        res.append({"niche": nm, "products": prods[:5]})
            except Exception:
                res = []
        return res

    def _dedup_new(items):
        """Garde les niches UNIQUES (signature) ET jamais proposees avant (anti-doublon runs)."""
        seen, keep = set(), []
        for it in items:
            sig = _niche_sig(it["niche"])
            if not sig or sig in seen:
                continue
            if _seen_before(it["niche"], hist_sigs):
                continue
            seen.add(sig); keep.append(it)
        return keep

    if e.ai_available():
        out = _gen_ai(excl_txt)
        # EXHAUSTION: si l'IA n'a renvoye QUE des niches deja vues (dedup vide alors qu'elle a
        # repondu), on RETENTE une fois en lui listant AUSSI ce qu'elle vient de proposer =>
        # evite l'ecran vide quand le registre est gros. Borne a 1 retry (pas de boucle infinie).
        if out and not _dedup_new(out):
            extra = excl_txt + ("; " if excl_txt else "") + ", ".join(it["niche"] for it in out)
            retry = _gen_ai(extra)
            if _dedup_new(retry):
                out = retry
    if not out:                          # repli deterministe: inverse produit->niche en niche->produits
        try:
            import demand_scan
            from collections import defaultdict
            grp = defaultdict(list)
            for kw, nm in demand_scan.PRODUCTS.items():
                if _is_banned(kw) or _is_banned(nm):
                    continue
                grp[nm].append(kw)
            out = [{"niche": nm, "products": ps[:5]} for nm, ps in grp.items()]
        except Exception:
            out = [{"niche": n2, "products": [e.NICHE_SEARCH_KW.get(n2, n2)]}
                   for n2 in e.NICHE_TAXONOMY if not _is_banned(n2)]
    # dedup par signature de tokens + EXCLUSION des niches deja proposees aux runs precedents
    dedup = _dedup_new(out)[:n]
    # memorise les niches RETENUES pour ne plus jamais les reproposer
    _niche_history_add([it["niche"] for it in dedup])
    return dedup


# CACHE DEMANDE (economie credits API): le nb d'annonces actives Etsy d'un mot-cle bouge a peine
# en 24h. On le persiste par mot-cle avec un TTL => les reruns ET les mots-cles qui se chevauchent
# entre niches ne reconsomment PAS de credit. TTL reglable via NF_DEMAND_TTL_H (def 24h).
DEMAND_CACHE_F = e.CACHE / "niche_demand.json"
_DEMAND_TTL = float(os.environ.get("NF_DEMAND_TTL_H", "24")) * 3600.0


def _demand_cache_load():
    try:
        return json.loads(DEMAND_CACHE_F.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _demand_cache_get(kw):
    import time
    d = _demand_cache_load().get((kw or "").strip().lower())
    if not d:
        return None
    if (time.time() - float(d.get("ts", 0))) > _DEMAND_TTL:
        return None                                  # perime => on re-interroge
    return int(d.get("count", 0))


def _demand_cache_put(kw, count):
    import time
    d = _demand_cache_load()
    d[(kw or "").strip().lower()] = {"count": int(count), "ts": time.time()}
    if len(d) > 5000:                                # borne la taille
        d = dict(sorted(d.items(), key=lambda kv: -kv[1].get("ts", 0))[:5000])
    tmp = DEMAND_CACHE_F.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tmp.replace(DEMAND_CACHE_F)
    except Exception:
        pass


def _demand_product(kw, api_ok):
    """Proxy DEMANDE d'UN produit = nb d'annonces actives Etsy. Cache 24h => 0 credit si deja vu
    recemment. 1 credit API au 1er appel (ou apres expiration). None si API indispo."""
    if not api_ok:
        return None
    cached = _demand_cache_get(kw)
    if cached is not None:
        return cached
    try:
        import urllib.parse
        d = e._get("/listings/active?limit=1&keywords=" + urllib.parse.quote(kw))
        c = int(d.get("count") or 0)
        _demand_cache_put(kw, c)
        return c
    except Exception:
        return None


def _demand_niche(products, api_ok):
    """DEMANDE de la NICHE = SOMME des annonces actives sur ses produits (pas 1 produit).
    Retourne (total|None, par_produit dict)."""
    per = {}
    tot = 0; got = False
    for p in products[:PRODUCTS_PER_NICHE]:
        c = _demand_product(p, api_ok)
        per[p] = c
        if c is not None:
            tot += c; got = True
    return (tot if got else None), per


def _sample_scrape(products, per_product, stop=None):
    """Echantillonnage LEGER en mode scrape: pour PLUSIEURS produits de la niche, on prend les
    1res boutiques de la page de recherche (search via CDP, ~10s) et on scrape leur catalogue
    (batch CDP). PAS de run_scrape (lourd: expansion mots-cles, budgets 10min, gate niche strict
    qui vidait 'cuisine'). Retourne (shops, found_total). Chaque shop = dict pret a juger."""
    import scraper
    merged = {}; found = 0
    for kw in (products[:PRODUCTS_PER_NICHE] or products[:1]):
        if e._stopped(stop):
            break
        kw_eff, _ = e.resolve_keyword(kw)
        try:
            res = scraper.scrape_search_shops(kw_eff, pages=1)
        except Exception:
            res = []
        names = [n for n, _ in res][:per_product]
        found += len(res)
        names = [n for n in names if n not in merged]
        if not names:
            continue
        try:
            cats = scraper.scrape_shops_batch(names)
        except Exception:
            cats = {}
        for n in names:
            d = cats.get(n) or {}
            titles = d.get("titles") or []
            if d.get("error") or not titles:
                continue
            merged[n] = {"id": n, "name": n, "titles": titles,
                         "images": d.get("images") or [], "country": d.get("country") or "",
                         "sold": d.get("sold"), "months": d.get("months"), "rate": d.get("rate", 0),
                         "url": "https://www.etsy.com/shop/" + n}
    return list(merged.values()), found


def _sample_api(products, total_target, filters, deep, mode, stop=None):
    """Echantillonnage mode API: reutilise run_discovery (API Etsy, PAS de Datadome => fiable) sur
    plusieurs produits, agrege. run_discovery juge DEJA les boutiques (orchestrate) => les shops
    reviennent avec ai_profile_drop / dropship_confirmed. deep=True => validation image activee."""
    prods = products[:PRODUCTS_PER_NICHE] or products[:1]
    per = max(1, -(-total_target // max(1, len(prods))))
    f = dict(filters or {})
    f.update({"use_ai": True, "keep_mixed": True, "min_per_niche": 1,
              "validate_ali": bool(deep), "ali_gate": bool(deep)})
    merged = {}; found = 0; en_niche = 0; confirmed = 0
    for p in prods:
        if e._stopped(stop):
            break
        kw_eff, _ = e.resolve_keyword(p)
        f["_query_raw"] = p; f["_query"] = kw_eff
        try: rem = int(e.quota_remaining())
        except Exception: rem = 5000
        mxa = min(per * 6 + 60, max(int(rem * 0.25), 120))
        try:
            res = e.run_discovery(keyword=kw_eff, target_count=per, max_api=mxa, filters=f, stop=stop)
        except Exception:
            continue
        for s in (res.get("shops") or []):
            k = str(s.get("id") or s.get("name") or "").lower()
            if k and k not in merged:
                merged[k] = s
        found += int(res.get("found") or 0)
        fn = res.get("funnel") or {}
        en_niche += int(fn.get("en_niche_apres_filtres") or fn.get("en_niche") or 0)
        confirmed += int(fn.get("match_niche_et_gate_drop") or fn.get("drop_confirme") or 0)
    shops = list(merged.values())
    return shops, found, (en_niche or len(shops)), confirmed


def _judge_profile(shops):
    """PHASE 1: juge le drop par PROFIL (IA titres -> ai_dropship, puis profile_drop_score qui
    combine coherence/age/pays). Aucun Lens. Pose ai_dropship + ai_profile_drop sur chaque shop."""
    if not shops:
        return
    verdict = e.ai_refine(shops)                 # pas de query: on veut le jugement DROP, pas la niche
    for s in shops:
        v = verdict.get(s["id"]) or {}
        if "dropship" in v:
            try: s["ai_dropship"] = round(float(v["dropship"]), 2)
            except Exception: pass
        s["ai_profile_drop"] = e.profile_drop_score(s)


def _judge_image(shops, filters, stop=None):
    """PHASE 2: PREUVE image (Lens+vision) + arbitre. Pose dropship_confirmed/dropship_confidence.
    Borne par le budget temps interne de validate_shops_ali."""
    if not shops:
        return
    import agents
    nprod = int((filters or {}).get("ali_products", 5) or 5)
    minm = int((filters or {}).get("ali_min_match", 2) or 2)
    try:
        e.validate_shops_ali(shops, nprod, minm, use_vision=e.vision_available(), stop=stop)
    except Exception:
        pass
    for s in shops:
        try:
            agents.profiler(s); agents.referee(s)
        except Exception:
            pass


def _profile_drop_rate(shops):
    """Fraction des boutiques echantillonnees qui SEMBLENT du drop d'apres le PROFIL (estimation)."""
    if not shops:
        return 0.0, 0
    n_drop = 0
    for s in shops:
        # On prend le MAX entre le jugement IA BRUT (ai_dropship = "produit industriel sourcable
        # AliExpress", le bon signal au niveau NICHE) et le profil (ai_profile_drop, plafonne par la
        # coherence/age). Sinon les niches de gadgets generiques mais a catalogue coherent (ex LED,
        # silicone) etaient sous-estimees (lues "artisan") => 0% a tort.
        vals = []
        for k in ("ai_dropship", "ai_profile_drop"):
            v = s.get(k)
            try:
                if v is not None: vals.append(float(v))
            except Exception: pass
        prof = max(vals) if vals else 0.0
        country = (s.get("country") or "").strip().upper()
        if prof >= DROP_PROFILE_MIN or country in e._DROPSHIP_COUNTRIES:
            n_drop += 1
    return n_drop / len(shops), n_drop


def scout_niches(filters=None, mode="scrape", n_candidates=15, sample_per_niche=6,
                 deep_top=3, progress=None, stop=None):
    """Coeur du NICHE FINDER. Evalue chaque NICHE (famille) en agregeant sur PLUSIEURS produits.
    Retourne {niches:[...], mode, candidates, deep_top, generated_by, quota_remaining}.
    Chaque niche: {niche, products, demand, demand_per, sampled, drop_est_pct, drop_n_est,
                   drop_proven_pct, drop_confirmed, examples, score, phase, demand_norm, found}."""
    f0 = dict(filters or {})
    api_ok = bool(e._load_ai_config().get("etsy")) or mode in ("api", "live")
    candidates = suggest_niches(n_candidates)
    generated_by = "ia" if e.ai_available() else "liste"

    def emit(obj):
        if progress:
            try: progress(obj)
            except Exception: pass

    emit({"type": "candidates", "niches": [c["niche"] for c in candidates], "mode": mode})

    # -------- PHASE 1: estimation RAPIDE (profil+IA, PAS de Lens), agregee sur les produits --------
    rows = []
    for i, c in enumerate(candidates):
        if e._stopped(stop):
            break
        emit({"type": "phase1", "i": i + 1, "total": len(candidates), "niche": c["niche"]})
        demand, demand_per = _demand_niche(c["products"], api_ok)
        if mode == "scrape":
            shops, found = _sample_scrape(c["products"], sample_per_niche, stop=stop)
            _judge_profile(shops)
        else:
            shops, found, _en, _conf = _sample_api(c["products"], sample_per_niche, f0, False, mode, stop=stop)
        rate, n_drop = _profile_drop_rate(shops)
        # HONNETETE: sampled==0 = on n'a PAS pu juger (scraping bloque / 429 ou catalogues vides).
        # On NE pretend PAS "0% drop" (trompeur). On marque la niche 'bloquee' => affichee a part,
        # PAS classee comme une niche a 0 drop.
        blocked = (len(shops) == 0)
        row = {"niche": c["niche"], "products": c["products"], "demand": demand,
               "demand_per": demand_per, "sampled": len(shops),
               "drop_est_pct": (None if blocked else round(rate * 100)),
               "drop_n_est": n_drop, "found": found, "blocked": blocked,
               "drop_proven_pct": None, "drop_confirmed": 0, "examples": [], "phase": 1,
               "_shops": shops}
        rows.append(row)
        emit({"type": "phase1_done", "niche": c["niche"], "demand": demand,
              "drop_est_pct": row["drop_est_pct"], "sampled": row["sampled"],
              "blocked": row["blocked"]})

    def _dem_val(r):
        return r["demand"] if r["demand"] is not None else r["found"]
    dmax = max([_dem_val(r) for r in rows] + [1])
    for r in rows:
        r["demand_norm"] = round(_dem_val(r) / dmax, 3) if dmax else 0.0
        est = (r["drop_est_pct"] or 0) / 100.0       # blocked => None => 0 (ne booste pas)
        r["score"] = round(100 * (0.5 * r["demand_norm"] + 0.5 * est))
    rows.sort(key=lambda r: -r["score"])

    # -------- PHASE 2: PREUVE image (Lens+vision) sur le TOP --------
    # On NE deep-teste QUE les niches qui ont du drop ESTIME (>0) et qui ne sont PAS bloquees
    # (sinon on cramerait du temps Lens sur des niches sans echantillon).
    top = [r for r in rows if not r.get("blocked") and (r["drop_est_pct"] or 0) > 0][:max(0, deep_top)]
    for j, r in enumerate(top):
        if e._stopped(stop):
            break
        emit({"type": "phase2", "i": j + 1, "total": len(top), "niche": r["niche"]})
        if mode == "scrape":
            # REUTILISE les boutiques deja scrapees en phase 1 (pas de re-scrape) => on ajoute la
            # PREUVE image (Lens+vision) + arbitre dessus. On CAPE le nombre de boutiques testees:
            # Lens est lent (~25s/boutique) et le budget temps est partage => sans cap, on testait
            # 1-2 boutiques sur 15 puis budget epuise => 0 confirme a tort. Cap = budget tient.
            shops = (r.get("_shops") or [])[:PHASE2_MAX_SHOPS]
            _judge_image(shops, f0, stop=stop)
            en_niche = len(shops)
            confirmed_shops = [s for s in shops if s.get("dropship_confirmed")]
            proven = len(confirmed_shops)
        else:
            shops, _found, en_niche, proven = _sample_api(r["products"], max(sample_per_niche, 8),
                                                          f0, True, mode, stop=stop)
            confirmed_shops = [s for s in shops if s.get("dropship_confirmed")]
            proven = proven or len(confirmed_shops)
        r["drop_confirmed"] = proven
        r["drop_proven_pct"] = round(100 * proven / en_niche) if en_niche else 0
        r["examples"] = [{"name": s.get("name"), "url": s.get("url"),
                          "conf": s.get("dropship_confidence")}
                         for s in confirmed_shops[:5]]
        r["phase"] = 2
        prov = (r["drop_proven_pct"] or 0) / 100.0
        r["score"] = round(100 * (0.4 * r["demand_norm"] + 0.6 * prov))
        emit({"type": "phase2_done", "niche": r["niche"],
              "drop_proven_pct": r["drop_proven_pct"], "drop_confirmed": r["drop_confirmed"]})

    rows.sort(key=lambda r: (-(2 if r["phase"] == 2 else 1), -r["score"]))
    for r in rows:
        r.pop("_shops", None)                    # ne pas serialiser les catalogues bruts dans le JSON
    _hist_sigs, _hist_names = _niche_history_load()
    return {"niches": rows, "mode": mode, "candidates": len(candidates),
            "deep_top": deep_top, "generated_by": generated_by,
            "niche_history_count": len(_hist_names),
            "quota_remaining": e._remaining.get("today") if isinstance(e._remaining, dict) else None}

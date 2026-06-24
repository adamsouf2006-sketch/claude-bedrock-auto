"""
EQUIPE IA — orchestration type "equipe de foot": un MENEUR (COACH) distribue le travail a des
agents EXECUTEURS, chacun avec UNE tache precise, puis un ARBITRE tranche. Objectif: une seule
direction claire (avant: logique drop eparpillee dans run_scrape/run_discovery), rapide
(court-circuit du hors-niche AVANT les checks couteux) et fiable (verdict + confiance unifies).

ROLES
  COACH (orchestrate)  : distribue, court-circuite, agrege, rend la liste finale triee.
  SCOUT  (agent_scout) : lit TOUS les titres -> in_niche? + nom de niche. Elimine le hors-niche.
  PROFILER (profiler)  : pays + age + coherence catalogue + jugement titres -> proba drop profil.
  CHASSEUR (chasseur)  : recherche par IMAGE (Lens/AliExpress) sur plusieurs photos -> produit trouve?
  JUGE   (vision)      : compare le DETAIL photo Etsy vs vignette AliExpress + detecte photos IA.
  ARBITRE (referee)    : combine TOUS les signaux -> verdict keep/reject + confiance 0..100 + raison.

Les agents reutilisent les fonctions eprouvees d'etsy_core (ai_refine, validate_shops_ali,
profile_drop_score...) — agents.py est la COUCHE CHEF D'ORCHESTRE par-dessus, pas une reecriture.
Import d'etsy_core fait en LAZY (etsy_core importe agents) pour eviter l'import circulaire.
"""

# Seuil de confiance dropship pour le gate strict (0..1). Reglable.
import os
DROP_GATE = float(os.environ.get("TEAM_DROP_GATE", "0.55"))


def _core():
    import etsy_core
    return etsy_core


# ------------------------------------------------------------------ SCOUT (niche)
def agent_scout(shops, f, stop=None):
    """Lit tout le catalogue, garde QUE les boutiques dans la niche cherchee, pose le nom de
    niche. Delegue a ai_enrich_shops (qui lit titre par titre et exige le match majoritaire).
    Retourne (survivants_niche, ai_used). Court-circuit: ce qui sort est deja en-niche."""
    c = _core()
    if not (f.get("use_ai", True) and c.ai_available()) or not shops:
        # pas d'IA: on ne peut pas juger la niche -> on laisse passer (le reste des filtres joue),
        # mais on marque le scout comme non-execute pour la transparence.
        for s in shops:
            s.setdefault("team", {})["scout"] = "IA off — niche non verifiee"
        return list(shops), False
    survivors, ai_used = c.ai_enrich_shops(shops, f, stop=stop)
    keep_ids = {id(s) for s in survivors}
    for s in shops:
        s.setdefault("team", {})["scout"] = (
            ("EN NICHE: " + (s.get("ai_niche") or "?")) if id(s) in keep_ids
            else "HORS-NICHE — elimine")
    return survivors, ai_used


# ------------------------------------------------------------------ PROFILER (drop profil)
def profiler(s):
    """Proba drop basee sur le PROFIL (pays/age/coherence/titres), independante de l'image.
    Retourne 0..1 ou None. Pose s['team']['profiler']."""
    c = _core()
    p = c.profile_drop_score(s)
    s.setdefault("team", {})["profiler"] = (
        ("profil drop %d%%" % round(100 * p)) if p is not None else "profil indecis")
    return p


# ------------------------------------------------- CHASSEUR (image) + JUGE (vision)
def chasseur_et_juge(shops, f, stop=None):
    """Recherche par image (Lens/AliExpress) sur plusieurs photos par boutique + comparaison
    DETAIL par la vision (si use_vision). Pose les champs ali_* sur chaque boutique. Retourne
    le nb d'appels API Etsy consommes (images mode API)."""
    c = _core()
    if not shops:
        return 0
    nprod = int(f.get("ali_products", 10) or 10)
    minm = int(f.get("ali_min_match", 2) or 2)
    api = c.validate_shops_ali(shops, nprod, minm, stop=stop,
                               use_vision=bool(f.get("use_vision")))
    for s in shops:
        hits = s.get("ali_hits")
        if s.get("ali_blocked"):
            tag = "image bloquee (captcha)"
        elif s.get("ali_validated"):
            tag = "produit TROUVE sur AliExpress (%s hits)" % (hits if hits is not None else "?")
        elif s.get("ali_validated") is None:
            tag = "image non testee"
        else:
            tag = "produit introuvable AliExpress"
        s.setdefault("team", {})["chasseur"] = tag
        if f.get("use_vision"):
            same = s.get("ali_detail_same", 0); diff = s.get("ali_detail_diff", 0)
            s["team"]["juge"] = "vision: %d meme produit, %d differents" % (same, diff)
    return api or 0


# ------------------------------------------------------------------ ARBITRE (referee)
def referee(s):
    """Combine TOUS les signaux des agents en UN verdict + UNE confiance 0..1. Deterministe
    (0 appel LLM, donc rapide et reproductible). Hierarchie des PREUVES (du + fort au + faible):
      1. vision 'meme produit' / page AliExpress confirmee  => preuve quasi-certaine.
      2. pays drop (CN/HK/TW/MO)                              => usine deguisee quasi-certaine.
      3. hash perceptuel verifie (vignette ~= photo)         => preuve forte.
      4. image trouvee (Lens validated)                      => preuve moyenne.
      5. profil seul (pays/age/coherence)                    => presomption (plafonnee).
    La vision qui dit 'produit DIFFERENT' (faux positif Lens) RABAISSE la confiance.
    Pose s['dropship_confidence'] (0..100), s['final_verdict'], s['final_reason'],
    s['team']['arbitre']. Retourne (confiance0_1, confirmed_bool)."""
    c = _core()
    page_ok = int(s.get("ali_page_confirmed") or 0)
    detail_same = int(s.get("ali_detail_same") or 0)
    detail_diff = int(s.get("ali_detail_diff") or 0)
    verified = int(s.get("ali_verified") or 0)
    validated = bool(s.get("ali_validated"))
    country = (s.get("country") or "").strip().upper()
    prof = s.get("ai_profile_drop")
    if prof is None:
        prof = profiler(s) or 0.0

    conf = 0.0; reason = "aucune preuve drop"
    if detail_same > 0 or page_ok > 0:
        conf = 0.95; reason = "meme produit confirme sur AliExpress (vision/page)"
    elif country in c._DROPSHIP_COUNTRIES:
        conf = 0.92; reason = "vendeur %s = usine/dropship deguise" % country
    elif verified > 0:
        conf = 0.80; reason = "vignette AliExpress ~= photo (hash verifie)"
    elif validated:
        conf = 0.65; reason = "produit trouve sur AliExpress (image)"
    else:
        # pas de preuve image -> on s'appuie sur le profil, MAIS plafonne (presomption, pas preuve)
        conf = min(float(prof) * 0.7, 0.50)
        reason = "profil suspect (%d%%) sans preuve image" % round(100 * float(prof))

    # la vision contredit (produit visuellement different) => faux positif Lens probable
    if detail_diff > 0 and detail_same == 0 and page_ok == 0:
        conf *= 0.5; reason = "image douteuse (vision: produit different)"
    # le profil remonte un peu une preuve image faible (coherence/pays/age concordants)
    conf = max(conf, min(float(prof), conf + 0.15)) if conf >= 0.5 else conf

    conf = round(max(0.0, min(1.0, conf)), 2)
    confirmed = conf >= DROP_GATE
    s["dropship_confidence"] = round(conf * 100)
    s["dropship_score100"] = s["dropship_confidence"]   # compat UI existante
    s["dropship_confirmed"] = confirmed
    s["final_verdict"] = "keep" if confirmed else "reject"
    s["final_reason"] = reason
    s.setdefault("team", {})["arbitre"] = "%s — %d%% (%s)" % (
        "GARDE" if confirmed else "JETE", s["dropship_confidence"], reason)
    return conf, confirmed


# ------------------------------------------------------------------ COACH (orchestrateur)
def orchestrate(shops, f, stop=None, progress=None):
    """MENEUR DE JEU. Distribue le travail dans l'ordre, court-circuite tot, agrege, rend la
    liste finale. Retourne dict:
      {shops: [...], niche_pool: [...], ai_used, ali_used, api, funnel}
    - shops      = sortie finale (strict: en-niche ET drop confirme si ali_gate).
    - niche_pool = toutes les boutiques en-niche (avec leur verdict, pour le funnel/affichage soft).
    Strict total (ali_gate=True, defaut): on ne rend QUE le drop confirme. Sinon on rend toute la
    niche, triee par confiance, sans jeter."""
    c = _core()
    n_in = len(shops)
    # 1) SCOUT — elimine le hors-niche (le reste des agents ne tourne QUE sur l'en-niche => vitesse)
    niche_pool, ai_used = agent_scout(shops, f, stop=stop)
    if progress:
        progress(0, len(niche_pool))

    ali_used = False; api = 0
    if f.get("validate_ali") and niche_pool and not c._stopped(stop):
        ali_used = True
        # 2) CHASSEUR + JUGE — preuve image sur CHAQUE boutique en-niche (plusieurs photos)
        api = chasseur_et_juge(niche_pool, f, stop=stop)
    # PROFILER (toujours, cheap) + 3) ARBITRE — verdict + confiance unifies
    for i, s in enumerate(niche_pool):
        if s.get("ai_profile_drop") is None:
            profiler(s)
        referee(s)
        if progress and (i % 3 == 0):
            progress(sum(1 for x in niche_pool[:i + 1] if x.get("dropship_confirmed")),
                     len(niche_pool))

    # 4) decision finale
    gate = f.get("ali_gate", True) and ali_used   # gate strict seulement si l'image a tourne
    if gate:
        final = [s for s in niche_pool if s.get("dropship_confirmed")]
    else:
        final = list(niche_pool)
    # tri: confiance drop desc, puis ventes/mois
    final.sort(key=lambda x: (-(x.get("dropship_confidence") or 0), -x.get("rate", 0)))
    funnel = {"scrapees_ou_recues": n_in, "en_niche": len(niche_pool),
              "drop_confirme": sum(1 for s in niche_pool if s.get("dropship_confirmed")),
              "affichees": len(final)}
    return {"shops": final, "niche_pool": niche_pool, "ai_used": ai_used,
            "ali_used": ali_used, "api": api, "funnel": funnel}

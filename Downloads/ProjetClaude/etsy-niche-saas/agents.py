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
    # VISION AUTO: le juge "meme objet" (couleurs/motifs/taille, photo ignoree) est ce qui
    # rattrape le drop a photo rebrandee. On l'ACTIVE par defaut des qu'un provider vision est
    # dispo (sauf si l'utilisateur a explicitement decoche use_vision, ou TEAM_VISION_AUTO=0).
    auto = (os.environ.get("TEAM_VISION_AUTO", "1") not in ("0", "false", "no")
            and c.vision_available())
    uv = bool(f.get("use_vision")) or auto
    api = c.validate_shops_ali(shops, nprod, minm, stop=stop, use_vision=bool(uv))
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
    # SIGNAL PHOTO (regle utilisateur: "les photos pas faites a l'IA = bon indicateur que ce
    # n'est PAS du drop"). ai_photo_drop ~ 0..1: haut = photos studio/IA/catalogue usine (indice
    # drop), bas = vraies photos artisan (decor reel, mains, imperfections => PAS drop). None=inconnu.
    photo = s.get("ai_photo_drop")
    try: photo = float(photo) if photo is not None else None
    except Exception: photo = None
    real_photos = (photo is not None and photo < 0.35)   # photos visiblement artisanales

    # PREUVE = PLUSIEURS produits (regle utilisateur: "pas avec une seule image"). Un SEUL
    # produit juge "meme" par la vision NE suffit PAS (faux positif Lens frequent). Il faut une
    # CORROBORATION: page produit AliExpress confirmee, OU >=2 produits independants juges
    # identiques (vision), OU >=2 hash verifies. Un signal isole = douteux, pas confirme.
    strong = (page_ok >= 1) or (detail_same >= 2) or (detail_same >= 1 and verified >= 1)
    medium = (verified >= 2) or (detail_same >= 1) or (verified >= 1 and validated)

    conf = 0.0; reason = "aucune preuve drop"
    if strong:
        conf = 0.90; reason = "meme produit retrouve sur AliExpress (plusieurs preuves)"
    elif country in c._DROPSHIP_COUNTRIES:
        conf = 0.88; reason = "vendeur %s = usine/dropship deguise" % country
    elif medium:
        conf = 0.50; reason = "1 produit ressemble a AliExpress (preuve unique insuffisante)"
    elif validated:
        conf = 0.45; reason = "image vaguement trouvee (non corrobore)"
    else:
        conf = min(float(prof) * 0.7, 0.45)
        reason = "profil suspect (%d%%) sans preuve image" % round(100 * float(prof))

    # la vision contredit (produit visuellement different) => faux positif Lens probable
    if detail_diff > detail_same and page_ok == 0:
        conf = min(conf, 0.40); reason = "image douteuse (vision: produits differents)"
    # VETO PHOTOS REELLES: si les photos sont clairement artisanales (pas IA/studio) ET qu'aucune
    # PAGE AliExpress n'est confirmee, on NE confirme PAS sur l'image seule (faux positif typique:
    # vrai artisan dont Lens a relie une photo a un produit AliExpress sans rapport). Sauf pays usine.
    if real_photos and page_ok == 0 and country not in c._DROPSHIP_COUNTRIES:
        conf = min(conf, 0.45); reason = "photos artisan reelles (pas IA/studio) => probablement PAS drop"
    # le profil remonte un peu une preuve image DEJA solide (jamais ne cree une preuve)
    if conf >= 0.55 and not real_photos:
        conf = max(conf, min(float(prof), conf + 0.10))

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
    drop_n = sum(1 for s in niche_pool if s.get("dropship_confirmed"))
    blocked_n = sum(1 for s in niche_pool if s.get("ali_blocked"))
    note = ""
    if gate:
        final = [s for s in niche_pool if s.get("dropship_confirmed")]
        # FILET "JAMAIS VIDE SI NICHE TROUVEE": si le gate drop ne confirme RIEN (Lens bloque par
        # captcha, ou produits custom non retrouves a l'image) MAIS qu'on a bien des boutiques EN
        # NICHE, on NE rend PAS un ecran vide. On montre les boutiques cuisine triees par confiance
        # drop, marquees below_threshold (drop non prouve par image) => l'utilisateur voit du concret
        # (toujours dans la niche) au lieu de "Aucune boutique" apres avoir scrape 200+ boutiques.
        if not final and niche_pool:
            for s in niche_pool:
                s["below_threshold"] = True
            final = list(niche_pool)
            note = ("Aucun drop CONFIRME par image (%s boutique(s) cuisine, Lens %s). "
                    "On affiche les boutiques de la niche triees par probabilite de drop "
                    "(non prouvee par image). Relance pour retenter la preuve image."
                    % (len(niche_pool),
                       "bloque par captcha" if blocked_n else "n'a pas retrouve les produits"))
    else:
        final = list(niche_pool)
    # tri: confiance drop desc, puis ventes/mois
    final.sort(key=lambda x: (-(x.get("dropship_confidence") or 0), -x.get("rate", 0)))
    funnel = {"scrapees_ou_recues": n_in, "en_niche": len(niche_pool),
              "drop_confirme": drop_n, "affichees": len(final)}
    return {"shops": final, "niche_pool": niche_pool, "ai_used": ai_used,
            "ali_used": ali_used, "api": api, "funnel": funnel, "note": note}

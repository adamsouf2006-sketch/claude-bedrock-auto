# Améliorations Niche Scout — session nuit (30 tours)

> ⚠️ **Le serveur live tourne l'ancien code.** Redémarre-le (`NicheScout-Stop.bat` puis `NicheScout.bat`,
> ou relance `python server.py`) pour activer ces changements.
>
> Tests : `python tests/test_suite.py` (offline, 0 quota). 129 checks, tous verts.

## Corrections de bugs réels
- **Crash Lens `KeyError`** (`ali_image._validate`) : si stop/budget coupait avant test d'un produit, l'agrégation plantait. → `.get` défensif.
- **`TypeError None > int`** (filtres `search_cache` + discovery) : une boutique d'âge inconnu (`months=None`) crashait le filtrage. → garde `is not None`.
- **`IndexError`** (`ali_image._build_detail`) : results vide → plantage. → fonction rendue totale ('none').
- **Collision dedup mode similar** : deux boutiques sans `id` partageaient la clé `"None"` → 2e jetée à tort. → clé id-sinon-nom.
- **Pollution `shown.json`** (tests) : les tests écrivaient dans le vrai registre. → registre isolé en temp.

## Anti-429 (req #3)
- **Retry-After** respecté (secondes + date HTTP), borné à 120s (anti-gel header géant).
- **Concurrence adaptative** : sur 429, tombe à 1 onglet + fenêtre de refroidissement, récupère seule.
- **Unifié sur les 2 moteurs scrape** (CDP **et** scrapling `_afetch`) : un 429 vu par une voie met l'autre en pause → plus de 429 entretenu. Soft-block (200+captcha) géré sur la voie CDP primaire.

## Niche finder — vrais chiffres + anti-doublon (req #4)
- **Anti-doublon entre runs** : registre persistant `cache/niche_history.json`, signature par tokens (ordre/pluriel/accents ignorés) → jamais 2× la même niche, ni reformulation. Prompt IA reçoit la liste à exclure.
- **Anti-écran-vide** : si l'IA ne renvoie que des doublons → 1 retry renforcé.
- **Cache demande 24h** (`cache/niche_demand.json`) : nb d'annonces actives Etsy mis en cache → reruns et mots-clés chevauchants = **0 crédit** (req #2 économie tokens).
- **UI** : tuile « déjà proposées » + lien « Réinitialiser l'historique des niches » (endpoint `/api/niche_finder_reset`).

## Performance (req #1)
- **`search_cache` ≈ 5.9× plus rapide** (steady-state ~1.3s vs ~7.7s sur 7058 boutiques), **0 changement de résultat** :
  - memo `_load` (clé mtime, copie superficielle = sûr en threads) → plus de re-parse JSON 7000 records par requête ;
  - `_ratio` lowercase chaque titre 1× (au lieu de N× dans le `any`) ;
  - `catalog_reject` early-exit bidirectionnel (atteint/ne peut plus atteindre le seuil) → ~3.85× sur ce filtre.
- *(Regex de mots-clés testée puis abandonnée : 3× plus lente que le substring C-level.)*

## Précision drop — faux positif vieil artisan (retour utilisateur)
- **Bug gate** : `ai_enrich_shops` gatait sur `ai_dropship` BRUT → un vieil artisan que l'IA surnote (ex 0.7) passait le gate `ai_dropship_gate`. → gate désormais sur `ai_profile_drop` (age-aware : >4 ans plafonné à 0.20). Vieux artisans exclus.
- **Défaut UI âge max** : passé de **120 → 24 mois**. Le drop = boutiques jeunes ; 120 (10 ans) laissait passer les vieux artisans par défaut (cause du bruit signalé). Réglable à la hausse si besoin.
- Vérifié OK (déjà age-aware) : verdict `referee`, affichage UI (`dropship_confidence`), gate mode similar.

## Tests
- Banc offline `tests/test_suite.py` (+ `tests/README.md`) : 129 checks couvrant niche finder (dedup, exhaustion, cache demande, pipeline complet), discover/cache, similar (parsing), détection drop (hash/grade/verdict/orchestrate), anti-429, routage serveur. 0 quota, 0 réseau, 0 régression sur 30 tours.

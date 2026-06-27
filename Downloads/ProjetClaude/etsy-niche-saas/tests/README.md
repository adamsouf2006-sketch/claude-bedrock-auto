# Banc de tests Niche Scout

Tests **offline** : aucun appel API Etsy, aucun token OpenRouter, aucun Chrome/réseau.
Mocks pour tout ce qui sort de la machine. Sûr à lancer en boucle, ne consomme **aucun quota**.

## Lancer

```bash
cd etsy-niche-saas
python tests/test_suite.py
```

Sortie : `==== N ok / 0 fail ====`. Code retour `0` = tout vert, `1` = au moins un échec
(le nom du test échoué est listé). Le cache réel n'est jamais touché (cache isolé en dossier temp,
registre `shown.json` redirigé).

## Couverture (par mode du logiciel)

| Zone | Ce qui est verrouillé |
|------|----------------------|
| **Niche finder** | dedup signature (ordre/pluriel/accents), anti-doublon **entre runs** (persistant), anti-écran-vide (retry exhaustion), cap historique, cache demande 24h (économie crédits), pipeline complet `scout_niches` mocké |
| **Discover / boutiques par niche** | `search_cache` bout-en-bout (HTTP), filtres `keyword_relevance`/`catalog_reject`, robustesse `months=None`, perf `_ratio_ge` == `_ratio>=seuil` |
| **Boutiques similaires (lien)** | `resolve_shop_name` tous formats d'URL + nom brut + input vide |
| **Détection drop (Lens+AliExpress)** | hash perceptuel (`_ham`/`_hash_dist`), `_grade` exact/strong/weak, `_dedup_unique`, `_precision_gate`, parse prix multi-devises, `_build_detail` total, robustesse `outcome.get` |
| **Verdict (agents)** | `referee` hiérarchie de preuves (forte/CN/veto photos réelles/profil seul/vision contredit), `orchestrate` funnel + tri + gate + filet jamais-vide |
| **Infra** | anti-429 (Retry-After + cap + concurrence adaptative), routage serveur, endpoint reset niches |

## Conventions

- Chaque check : `check("nom", condition, detail)`. Un échec n'arrête pas la suite (tout est exécuté).
- Les mocks restaurent l'état global (`ai_available`, `_get`, `_load`, `SHOWN_F`...) en `finally`.
- Pour ajouter un test : écrire une fonction `test_*`, l'ajouter au tuple dans `main()`.

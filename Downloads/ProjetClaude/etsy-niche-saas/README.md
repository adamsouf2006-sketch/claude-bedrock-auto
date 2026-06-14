# NicheScout — SaaS de recherche de niches Etsy (style Alura)

Trouve des **boutiques Etsy jeunes qui cartonnent** et déduit les **niches** par clustering.
Données réelles via l'**API Etsy v3** (ventes totales exactes, date d'ouverture, % produits digitaux, pays).

## Lancer
```bash
cd etsy-niche-saas
python server.py
# ouvre http://localhost:8000
```
Aucune dépendance (Python 3 stdlib uniquement).

## Fonctions
- **Découverte** : par mot-clé OU sans mot-clé (= nouveautés Etsy toutes niches, vraie découverte agnostique).
- **Filtres** : ventes/mois min, âge (min/max mois), ventes totales min, exclusion digital, exclusion personnalisé, exclusion boutiques "custom requests", exclusion catégories (digital, vêtements, bijoux, stickers/porte-clés, croyance, électronique, plantes).
- **Clustering** : regroupe automatiquement les boutiques retenues en niches (≈20 thèmes produit) — la niche est **déduite après**, pas tapée avant.
- **Quota** : affiche le quota API restant (5000/jour) ; cache disque (`cache/shops.json`) pour ne pas re-payer une boutique déjà vue.

## Logique métier
`etsy_core.py` :
- `discover_shop_ids(keyword, pages)` → listings/active triés par date → shop_ids.
- `enrich_shop(id)` → ventes/mois = `transaction_sold_count / âge_mois`.
- `passes_filters(rec, filtres)` → applique tous les critères.
- `cluster(shops)` → niches par mots-clés produit.

## Coût API
1 requête par page de découverte (100 boutiques) + 1 requête par boutique enrichie.
Règle `max_enrich` pour borner la consommation. Boutiques déjà en cache = 0 requête.

## Activer l'IA (classification intelligente des niches)
Par défaut le classement des niches est déterministe (vote par mots-clés sur **tous les titres**
de chaque boutique). Pour un classement IA (juge acceptabilité + nomme la niche précise sur le
catalogue complet, 1 seul appel LLM pour tout le lot) :

```cmd
set ANTHROPIC_API_KEY=sk-ant-xxxxx
python server.py
```
Puis coche **« 🤖 Raffinage IA »** dans l'UI. Sans clé, l'option est ignorée proprement.

## Logique de niche (important)
La niche d'une boutique est jugée sur **l'ensemble de ses titres produits** (jusqu'à ~24 en base),
pas sur un seul. Vote par titre : le thème qui matche le plus de titres gagne ; en dessous d'un
seuil de confiance → « Autres ». Filtre **personnalisé** : une boutique est rejetée si ≥34 % de son
catalogue est personnalisé. Mots-clés de thèmes **spécifiques** (ex : `suncatcher` ≠ `cat`) pour
éviter les faux classements.

## Étendre
- Ajouter des thèmes : `THEMES` dans `etsy_core.py`.
- Ajouter des catégories bannies : `BAN_KW`.
- Export CSV / scoring de niche / volume de recherche : prochaines itérations.

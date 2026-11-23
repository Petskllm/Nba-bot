# 🏀 Nba-bot — pronos de props NBA

Bot qui construit chaque soir des pronos de props joueurs NBA (points, rebonds, passes, PRA, paniers à 3 pts)
à partir de sources **100 % gratuites** (API ESPN + rapport officiel des blessures NBA), puis publie :

- des **pronos solides** (6 max, proba >= 0.62)
- un **ticket safe** (1 à 3 sélections très sûres)
- un **combiné fun** (cote visée >= 30, sélections à 50 %+)

## Fonctionnement

1. `Nba_bot.py` (cron GitHub Actions, 22 h UTC) :
   - recharge les box scores de la saison (cache `data_nba/box_nba.json`) ;
   - lit les props DraftKings via l'API publique ESPN ;
   - croise avec le rapport officiel des blessures NBA (PDF, toutes les 15 min) ;
   - calcule les probas (binomiale négative, calibration 0.92, mix modèle/marché 65/35) ;
   - applique les filtres de rôle (minutes L5, stabilité, titulaire, hit-rate L10, blowout) ;
   - publie les tickets sur Telegram et journalise chaque sélection dans `historique_nba.json`.

## Secrets requis

| Secret | Rôle |
|---|---|
| `TELEGRAM_TOKEN` | token du bot Telegram |
| `TELEGRAM_CHAT_ID` (ou `CHAT_ID`) | canal de publication |

## Lancer en local

```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=...
python Nba_bot.py

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backtest de l'historique du bot NBA.

Lit historique_nba.json et calcule, par ticket et par marché :
  - nombre de picks tranchés (gagnés/perdus) et hit-rate réel ;
  - proba moyenne annoncée (dérive de calibration : annonce - réel) ;
  - Brier score (qualité des probas, 0 = parfait) ;
  - ROI à mise fixe (1 unité par pick, gain = cote - 1 si gagné).

Usage :
  python backtest.py [--fichier historique_nba.json] [--min 20] [--json]
"""
import argparse
import json
import sys
from collections import defaultdict

TICKETS = ("solide", "safe", "fun")


def picks_tranches(picks):
    return [p for p in picks if p.get("res") in ("gagne", "perdu")]


def bilan(picks):
    n = len(picks)
    if not n:
        return None
    hits = sum(1 for p in picks if p["res"] == "gagne")
    mise = float(n)
    profit_gains = sum((p["cote"] - 1) for p in picks if p["res"] == "gagne")
    mises_perdues = sum(1 for p in picks if p["res"] == "perdu")
    profit = profit_gains - mises_perdues
    brier = sum((1 - p["p"]) ** 2 for p in picks if p["res"] == "gagne") \
        + sum(p["p"] ** 2 for p in picks if p["res"] == "perdu")
    return {
        "n": n,
        "hits": hits,
        "hit_rate": hits / n,
        "proba_annoncee": sum(p["p"] for p in picks) / n,
        "brier": brier / n,
        "roi": profit / mise,
        "profit": profit,
    }


def affiche(titre, b):
    der = b["proba_annoncee"] - b["hit_rate"]
    print("%-22s n=%4d  hit=%5.1f%%  annonce=%5.1f%%  derive=%+5.1f pts  Brier=%.3f  ROI=%+6.1f%%  P/L=%+.2f u"
          % (titre, b["n"], b["hit_rate"] * 100, b["proba_annoncee"] * 100,
             der * 100, b["brier"], b["roi"] * 100, b["profit"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fichier", default="historique_nba.json")
    ap.add_argument("--min", type=int, default=20, help="picks tranchés min pour afficher un groupe")
    ap.add_argument("--json", action="store_true", help="sortie JSON")
    args = ap.parse_args()

    try:
        with open(args.fichier, encoding="utf-8") as f:
            picks = picks_tranches(json.load(f)["picks"])
    except Exception as e:
        sys.exit("Impossible de lire %s : %s" % (args.fichier, e))

    groupes = {"global": picks}
    for t in TICKETS:
        groupes["ticket:" + t] = [p for p in picks if p.get("ticket") == t]
    par_m = defaultdict(list)
    for p in picks:
        par_m[p.get("m", "?")].append(p)
    for m, ps in par_m.items():
        groupes["marche:" + str(m)] = ps
    par_tm = defaultdict(list)
    for p in picks:
        par_tm[(p.get("ticket"), p.get("m"))].append(p)
    for (t, m), ps in sorted(par_tm.items()):
        groupes["%s/%s" % (t, m)] = ps

    resultats = {k: bilan(v) for k, v in groupes.items()}
    resultats = {k: v for k, v in resultats.items() if v and v["n"] >= args.min}

    if args.json:
        print(json.dumps(resultats, indent=1, ensure_ascii=False))
    else:
        print("")
        print("%d picks tranches au total (fichier %s, minimum %d par groupe)"
              % (len(picks), args.fichier, args.min))
        print("")
        for k in sorted(resultats, key=lambda k: -resultats[k]["n"]):
            affiche(k, resultats[k])
        print("")
        print("'derive' = proba annoncee - hit reel : positif = le bot est trop confiant.")
        print("")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
BOT NBA · PROPS JOUEURS · v1.2
Marchés : points, rebonds, passes, PRA (points+rebonds+passes), paniers à 3 points.
Tickets : pronos solides, combiné fun (grosse cote), ticket safe + texte prêt pour X.
v1.1 : filtre minutes stables, titulaires, retours de blessure (bilan 20-21 nov).
v1.2 : le combiné fun vise une cote 30+ avec des jambes confirmées (rôle / minutes / forme).
       Si 30 n'est pas atteignable proprement, on publie le meilleur ticket solide, sans pile-ou-face.

100 % gratuit, aucune clé API :
  - ESPN (JSON public) : calendrier, box scores, spreads/totaux, blessures, et les vraies
    lignes de props DraftKings (paliers « 15+ points »… = « Plus de 14.5 » sur Stake).
  - Rapport officiel des blessures NBA (PDF, mis à jour toutes les 15 min).
  - The Odds API N'EST PAS utilisée (les props coûteraient ~45 crédits par soir).
Données calculées par le bot (box scores de la saison, gardés dans data_nba/) :
  minutes, forme 5/10 matchs, moyenne saison, usage, défense adverse par poste, rythme (pace),
  domicile/extérieur, back-to-back, risque de « blowout » (spread), coéquipiers absents,
  stabilité des minutes et statut de titulaire.
"""
import json
import math
import os
import re
import subprocess
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

try:
    from zoneinfo import ZoneInfo
    TZ_ET = ZoneInfo("America/New_York")
    TZ_PARIS = ZoneInfo("Europe/Paris")
except Exception:                       # pragma: no cover
    TZ_ET = timezone(timedelta(hours=-5))
    TZ_PARIS = timezone(timedelta(hours=1))

# ====================== RÉGLAGES ======================
COTE_MIN = 1.20                 # aucune sélection sous cette cote (tous les tickets)

SOLIDES_MAX = 6                 # pronos solides affichés
SOLIDES_PROBA_MIN = 0.62        # chance estimée minimale
SOLIDES_COTE_MIN = 1.30
SOLIDES_VALUE_MIN = 0.02        # proba x cote >= 1.02
SOLIDES_MAX_PAR_MATCH = 2

FUN_COTE_CIBLE = 30.0           # v1.2 : cote totale visée (30 minimum, plus de cible 50 ni 8)
FUN_LEGS = (4, 5, 6, 7)         # jusqu'à 7 sélections, 1 par match ; un soir à 6-7 matchs peut viser 30
FUN_LEGS_REPLI = 3              # s'il n'y a que 3 matchs propres, on publie un ticket court plutôt que rien
FUN_COTE_LEG = (1.65, 8.0)      # lignes un peu plus hautes (souvent 1.80-2.50) pour construire la cote
FUN_PROBA_LEG_MIN = 0.50        # v1.2 : 50 % (plus de pile-ou-face à 22 %) ; 45 % trop fragile
FUN_VALUE_MIN = -0.04           # le modèle ne doit pas être (beaucoup) moins optimiste que le book
FUN_CHOIX_PAR_MATCH = 12
FUN_HITS_MIN = 6                # hits min sur les 10 derniers (si au moins 8 matchs)

SAFE_PROBA_MIN = 0.80
SAFE_COTE_MAX = 1.55
SAFE_LEGS_MAX = 3
SAFE_PROBA_3E = 0.84            # 3e sélection seulement si elle est très sûre

MINUTES_MIN = 22                # minutes projetées minimum (premier filet, tous tickets)
MATCHS_MIN = 5                  # matchs joués minimum cette saison
FENETRE_HEURES = 16             # matchs qui commencent dans les 16 prochaines heures

# v1.1 — filtres rôle / minutes (après le bilan du 20-21 novembre)
MINUTES_SOLIDE_SAFE = 26.0      # moyenne des 5 derniers matchs (pronos solides + ticket safe)
MINUTES_FUN = 26.0              # v1.2 : même exigence que les solides / le safe
MINUTES_PLANCHER_L5 = 18.0      # aucun des 5 derniers matchs sous 18 minutes
MINUTES_STD_MAX = 6.0           # écart-type (population) des 5 derniers
TROUS_MAX_SOLIDE_SAFE = 0       # aucun match d'équipe manqué sur les 5 derniers (retour blessure)
TROUS_MAX_FUN = 0               # v1.2 : plus de retour d'absence dans le fun
SAFE_BLOWOUT_SPREAD = 10.5      # ticket safe : pas de favori à -10.5 ou plus
FUN_TPM_BLOWOUT = 12.5          # fun : pas de 3-pts si favori à -12.5 ou plus
SOLIDES_TPM_BLOWOUT = 10.5      # solides : pas de 3-pts si favori à -10.5 ou plus
SOLIDES_HITS_MIN = 7            # hits min sur les 10 derniers (si au moins 8 matchs)
SAFE_HITS_MIN = 8
MOTS_MINUTES_LIMITEES = (
    "minutes restriction", "minute restriction", "will be limited", "limited minutes",
    "minutes limit", "on a minutes", "maintenance", "first game back", "just returned",
    "returning from", "return from injury", "re-evaluation",
)

CALIBRATION = 0.92              # mesuré : 94 % annoncés -> 89 % réels avant correction
POIDS_MODELE = 0.65             # proba finale = 65 % modèle + 35 % marché (cote DK sans marge)
MARGE_BOOK = 0.05               # marge moyenne DK sur les paliers
MARGE_ESTIM = 0.07              # marge ajoutée pour une cote estimée
ECART_MAX_MARCHE = 0.25         # modèle et cote DK en désaccord de plus de 25 pts : sélection écartée
                                # (ligne douteuse ou info que le modèle n'a pas : blessure, minutes…)

DISPERSION_DEF = {"pts": 2.0, "reb": 1.5, "ast": 1.5, "tpm": 1.25, "pra": 2.3}
DISPERSION_BORNES = (1.0, 3.5)
BORNES_DEF = (0.92, 1.08)       # défense adverse par poste
BORNES_ENV = (0.94, 1.06)       # rythme / total du match
BORNES_DOM = (0.96, 1.04)       # domicile / extérieur
BOOST_ABSENTS_MAX = 0.15        # coéquipiers absents : +15 % maximum
PART_REDISTRIBUEE = 0.55        # part des stats d'un absent redistribuée aux autres
B2B_MINUTES = 0.97              # back-to-back : -3 % de minutes
B2B_ADV = 1.015                 # adversaire en back-to-back : +1,5 %
BLOWOUT = ((12.5, 0.93), (9.5, 0.96))   # |spread| >= 12.5 : -7 % de minutes pour les titulaires

BILAN_JOURS = 30
HISTORIQUE = "historique_nba.json"
DOSSIER = "data_nba"
CACHE_BOX = os.path.join(DOSSIER, "box_nba.json")
WORKERS = 6
# ======================================================

ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ESPN_CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"
NBA_INJ_PAGE = "https://official.nba.com/nba-injury-report-{s}-season/"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

MARCHES = {
    "Points Milestones": "pts",
    "Rebounds Milestones": "reb",
    "Assists Milestones": "ast",
    "Points + Assists + Rebounds Milestones": "pra",
    "3-Point Field Goals Milestones": "tpm",
}
TYPES_PALIERS = {"218": "pts", "220": "reb", "219": "ast", "225": "pra", "221": "tpm"}
TYPES_LIGNE = {"1": "pts", "3": "reb", "2": "ast", "90": "pra", "4": "tpm"}   # over/under principal
NOM_MARCHE = {"pts": "points", "reb": "rebonds", "ast": "passes", "pra": "PRA (pts+reb+pas)", "tpm": "paniers à 3 pts"}
COURT_MARCHE = {"pts": "pts", "reb": "reb", "ast": "pas", "pra": "PRA", "tpm": "3pts"}
EMOJI_MARCHE = {"pts": "🎯", "reb": "🧱", "ast": "🤝", "pra": "📦", "tpm": "👌"}
PLANCHER_ESTIM = {"pts": 8, "reb": 4, "ast": 3, "tpm": 1, "pra": 15}
STATUTS_EXCLUS = ("out", "doubtful", "questionable", "suspension", "suspended")
JOURS = ["LUNDI", "MARDI", "MERCREDI", "JEUDI", "VENDREDI", "SAMEDI", "DIMANCHE"]
MOIS = ["", "JANVIER", "FÉVRIER", "MARS", "AVRIL", "MAI", "JUIN", "JUILLET", "AOÛT",
        "SEPTEMBRE", "OCTOBRE", "NOVEMBRE", "DÉCEMBRE"]

_session = requests.Session()
_session.headers.update(UA)


# ---------------------------------------------------------------- HTTP
def get_json(url, params=None, essais=3, timeout=20):
    for i in range(essais):
        try:
            r = _session.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (400, 404):
                return None
        except Exception as e:
            if i == essais - 1:
                print("Échec", url, e)
        time.sleep(1.5 * (i + 1))
    return None


def ref_id(ref, mot):
    m = re.search(mot + r"/(\d+)", ref or "")
    return m.group(1) if m else None


def norm_nom(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", " ", s)
    return re.sub(r"[^a-z]", "", s)


def num(x, defaut=0.0):
    try:
        return float(x)
    except Exception:
        return defaut


def paire(x):
    try:
        a, b = str(x).split("-")
        return int(a), int(b)
    except Exception:
        return 0, 0


# ---------------------------------------------------------------- BOX SCORES (cache saison)
def charger_cache():
    try:
        with open(CACHE_BOX, encoding="utf-8") as f:
            c = json.load(f)
        c.setdefault("games", {})
        c.setdefault("noms", {})
        c.setdefault("dates_ok", [])
        return c
    except Exception:
        return {"saison": None, "games": {}, "noms": {}, "dates_ok": []}


def sauver_cache(c):
    os.makedirs(DOSSIER, exist_ok=True)
    with open(CACHE_BOX, "w", encoding="utf-8") as f:
        json.dump(c, f, separators=(",", ":"))


def scoreboard(jour):
    """jour = date ET (date). Retourne la liste brute des events ESPN."""
    d = get_json(f"{ESPN_SITE}/scoreboard", {"dates": jour.strftime("%Y%m%d")})
    return d if d else {}


def pos_groupe(p):
    p = (p or "").upper()
    if p in ("C", "FC") or p.startswith("C"):
        return "C"
    if p in ("G", "PG", "SG", "GF") or p.startswith("G") or p.endswith("G"):
        return "G"
    return "F"


def compacter_resume(gid, d):
    """Résumé ESPN -> enregistrement compact (None si le match n'est pas terminé)."""
    try:
        comp = d["header"]["competitions"][0]
        if not comp["status"]["type"].get("completed"):
            return None, {}
        jour = (datetime.fromisoformat(comp["date"].replace("Z", "+00:00"))
                .astimezone(TZ_ET).date().isoformat())
        rec = {"d": jour, "h": None, "a": None, "sc": {}, "t": {}, "p": []}
        for c in comp["competitors"]:
            rec["h" if c["homeAway"] == "home" else "a"] = c["team"]["id"]
            rec["sc"][c["team"]["id"]] = int(num(c.get("score")))
        for t in d["boxscore"]["teams"]:
            st = {x["name"]: x.get("displayValue") for x in t.get("statistics", [])}
            fgm, fga = paire(st.get("fieldGoalsMade-fieldGoalsAttempted"))
            ftm, fta = paire(st.get("freeThrowsMade-freeThrowsAttempted"))
            rec["t"][t["team"]["id"]] = {
                "fga": fga, "fta": fta, "oreb": int(num(st.get("offensiveRebounds"))),
                "to": int(num(st.get("totalTurnovers", st.get("turnovers")))),
                "reb": int(num(st.get("totalRebounds"))), "ast": int(num(st.get("assists"))),
            }
        noms = {}
        for bloc in d["boxscore"]["players"]:
            tid = bloc["team"]["id"]
            s = bloc["statistics"][0]
            k = s.get("keys") or []
            idx = {n: i for i, n in enumerate(k)}
            for a in s.get("athletes", []):
                ath = a["athlete"]
                aid = ath["id"]
                pos = (ath.get("position") or {}).get("abbreviation", "F")
                noms[aid] = [ath.get("displayName", aid), pos, tid]
                vals = a.get("stats") or []
                if a.get("didNotPlay") or not vals:
                    continue

                def v(n):
                    return vals[idx[n]] if n in idx and idx[n] < len(vals) else "0"
                mins = num(v("minutes"))
                if mins <= 0:
                    continue
                fgm, fga = paire(v("fieldGoalsMade-fieldGoalsAttempted"))
                tpm, tpa = paire(v("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
                ftm, fta = paire(v("freeThrowsMade-freeThrowsAttempted"))
                rec["p"].append([aid, tid, pos_groupe(pos), mins, int(num(v("points"))),
                                 int(num(v("rebounds"))), int(num(v("assists"))), tpm, fga, fta,
                                 int(num(v("turnovers"))), 1 if a.get("starter") else 0])
        return rec, noms
    except Exception as e:
        print("Résumé illisible", gid, e)
        return None, {}


def maj_cache(cache, aujourdhui_et, debut_saison):
    """Ajoute au cache tous les matchs terminés de la saison (seulement les nouveaux)."""
    if cache.get("saison") != debut_saison.isoformat():
        cache.update({"saison": debut_saison.isoformat(), "games": {}, "noms": {}, "dates_ok": []})
    faits = set(cache["dates_ok"])
    jour = debut_saison
    a_charger = []
    jours_scannes = []
    while jour < aujourdhui_et:
        iso = jour.isoformat()
        if iso not in faits:
            jours_scannes.append(jour)
        jour += timedelta(days=1)

    def scan(j):
        sb = scoreboard(j)
        return j, sb.get("events") if sb else None

    with ThreadPoolExecutor(WORKERS) as ex:
        for j, evs in ex.map(scan, jours_scannes):
            if evs is None:
                continue
            tous_finis = True
            for e in evs:
                if (e.get("season") or {}).get("type") not in (2, 3, 5):
                    continue
                if not e["competitions"][0]["status"]["type"].get("completed"):
                    tous_finis = False
                    continue
                if e["id"] not in cache["games"]:
                    a_charger.append(e["id"])
            if tous_finis:
                faits.add(j.isoformat())

    def charge(gid):
        return gid, get_json(f"{ESPN_SITE}/summary", {"event": gid}, timeout=30)

    if a_charger:
        print(f"Box scores à charger : {len(a_charger)}")
    with ThreadPoolExecutor(WORKERS) as ex:
        for gid, d in ex.map(charge, a_charger):
            if not d:
                continue
            rec, noms = compacter_resume(gid, d)
            if rec:
                cache["games"][gid] = rec
                cache["noms"].update(noms)
    cache["dates_ok"] = sorted(faits)
    return cache


# ---------------------------------------------------------------- STATISTIQUES DÉRIVÉES
STAT_IDX = {"pts": 4, "reb": 5, "ast": 6, "tpm": 7}


def val_stat(row, m):
    if m == "pra":
        return row[4] + row[5] + row[6]
    return row[STAT_IDX[m]]


def construire_index(cache):
    """joueurs -> liste de matchs ; équipes -> matchs ; défense par poste ; pace ; moyennes ligue."""
    joueurs, equipes = {}, {}
    def_pos = {}          # (équipe défensive, poste) -> [min, pts, reb, ast, tpm, pra]
    ligue_pos = {}        # poste -> [min, pts, reb, ast, tpm, pra]
    pace = {}             # équipe -> [poss...]
    pts_eq = []
    for gid, g in cache["games"].items():
        h, a = g["h"], g["a"]
        poss = {}
        for tid, t in g["t"].items():
            poss[tid] = t["fga"] + 0.44 * t["fta"] - t["oreb"] + t["to"]
        if len(poss) == 2:
            p = sum(poss.values()) / 2
            for tid in poss:
                pace.setdefault(tid, []).append((g["d"], p))
        for tid in (h, a):
            adv = a if tid == h else h
            equipes.setdefault(tid, []).append({"gid": gid, "d": g["d"], "dom": tid == h, "adv": adv})
            if tid in g["sc"]:
                pts_eq.append(g["sc"][tid])
        tot_eq = {}
        for row in g["p"]:
            aid, tid = row[0], row[1]
            adv = a if tid == h else h
            joueurs.setdefault(aid, []).append({"gid": gid, "d": g["d"], "tid": tid, "dom": tid == h,
                                                "adv": adv, "row": row})
            te = tot_eq.setdefault(tid, [0.0, 0.0])
            te[0] += row[8] + 0.44 * row[9] + row[10]
            te[1] += row[3]
            vals = [row[3], row[4], row[5], row[6], row[7], row[4] + row[5] + row[6]]
            for cle, dico in (((adv, row[2]), def_pos), (row[2], ligue_pos)):
                acc = dico.setdefault(cle, [0.0] * 6)
                for i, v in enumerate(vals):
                    acc[i] += v
    for l in joueurs.values():
        l.sort(key=lambda x: x["d"])
    for l in equipes.values():
        l.sort(key=lambda x: x["d"])
    pace_moy = {t: sum(p for _, p in l[-15:]) / len(l[-15:]) for t, l in pace.items() if l}
    ligue_pace = sum(pace_moy.values()) / len(pace_moy) if pace_moy else 100.0
    return {
        "joueurs": joueurs, "equipes": equipes, "def_pos": def_pos, "ligue_pos": ligue_pos,
        "pace": pace_moy, "ligue_pace": ligue_pace,
        "ligue_pts": (sum(pts_eq) / len(pts_eq)) if pts_eq else 115.0, "cache": cache,
    }


def facteur_defense(idx, adv, pos, m):
    """Ce que l'adversaire accorde à ce poste (par minute) vs la moyenne de la ligue, lissé."""
    col = {"pts": 1, "reb": 2, "ast": 3, "tpm": 4, "pra": 5}[m]
    d = idx["def_pos"].get((adv, pos))
    lg = idx["ligue_pos"].get(pos)
    if not d or not lg or lg[0] <= 0 or d[0] <= 0:
        return 1.0
    taux_lg = lg[col] / lg[0]
    if taux_lg <= 0:
        return 1.0
    k_min = 600.0                         # ~ 6 matchs de minutes "moyennes" ajoutées
    taux = (d[col] + k_min * taux_lg) / (d[0] + k_min)
    f = 1 + 0.6 * (taux / taux_lg - 1)
    return min(max(f, BORNES_DEF[0]), BORNES_DEF[1])


def a_joue_la_veille(idx, tid, jour_et):
    veille = (jour_et - timedelta(days=1)).isoformat()
    return any(x["d"] == veille for x in idx["equipes"].get(tid, [])[-3:])


# ---------------------------------------------------------------- LOIS DE PROBA
def proba_au_moins(k, moy, disp):
    """P(X >= k) avec une binomiale négative (variance = disp x moyenne). Poisson si disp ~ 1."""
    if k <= 0:
        return 1.0
    if moy <= 0:
        return 0.0
    k = int(k)
    if disp <= 1.02:
        lp = -moy
        cdf = 0.0
        term = math.exp(lp)
        for i in range(k):
            cdf += term
            term *= moy / (i + 1)
        return max(0.0, min(1.0, 1 - cdf))
    r = moy / (disp - 1)
    p = 1 / disp                           # proba de "succès" de la NB
    cdf = 0.0
    for i in range(k):
        lpmf = (math.lgamma(i + r) - math.lgamma(r) - math.lgamma(i + 1)
                + r * math.log(p) + i * math.log(1 - p))
        cdf += math.exp(lpmf)
    return max(0.0, min(1.0, 1 - cdf))


def moyenne(l):
    return sum(l) / len(l) if l else 0.0


# ---------------------------------------------------------------- BLESSURES
def blessures_espn():
    """{norm_nom: (statut, commentaire)} et {athlete_id: statut}."""
    d = get_json(f"{ESPN_SITE}/injuries") or {}
    par_nom, par_id = {}, {}
    for eq in d.get("injuries", []):
        for inj in eq.get("injuries", []):
            ath = inj.get("athlete") or {}
            nom = ath.get("displayName", "")
            st = (inj.get("status") or "").strip()
            com = inj.get("shortComment") or ""
            par_nom[norm_nom(nom)] = (st, com)
            for l in ath.get("links", []):
                aid = ref_id(l.get("href"), "id")
                if aid:
                    par_id[aid] = (st, com)
                    break
    return par_nom, par_id


RE_INJ = re.compile(
    r"([A-Z][A-Za-z'\.\-]+(?: [A-Za-z'\.\-]+){0,2}),\s*([A-Z][A-Za-z'\.\-]+(?: [A-Za-z'\.\-]+){0,2})\s+"
    r"(Out|Questionable|Doubtful|Probable|Available)\b")


def texte_pdf(contenu):
    """Texte d'un PDF : pdftotext si présent, sinon pypdf."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(contenu)
        chemin = f.name
    try:
        try:
            out = subprocess.run(["pdftotext", "-layout", chemin, "-"], capture_output=True, timeout=60)
            if out.returncode == 0 and out.stdout:
                return out.stdout.decode("utf-8", "ignore")
        except Exception:
            pass
        try:
            from pypdf import PdfReader
            r = PdfReader(chemin)
            return "\n".join(p.extract_text(extraction_mode="layout") or "" for p in r.pages)
        except Exception as e:
            print("PDF blessures illisible :", e)
            return ""
    finally:
        os.unlink(chemin)


def parser_rapport_officiel(texte):
    res = {}
    for ligne in texte.splitlines():
        ligne = re.sub(r"(?<=\S) {2}(?=\S)", " ", ligne)       # pypdf : "Furphy,  Johnny" -> 1 espace
        ligne = re.sub(r"\s+(Jr\.?|Sr\.?|II|III|IV|V),", r" \1,", ligne)   # "Udeh   Jr.,Ernest"
        for m in RE_INJ.finditer(re.sub(r",\s{2,}", ", ", ligne)):
            nom, prenom, st = m.group(1).strip(), m.group(2).strip(), m.group(3)
            res[norm_nom(prenom + " " + nom)] = st
    return res


def blessures_officielles(maintenant):
    """Rapport officiel NBA le plus récent : {norm_nom: statut}. {} si indisponible."""
    an = maintenant.year if maintenant.month >= 8 else maintenant.year - 1
    saison = f"{an}-{str(an + 1)[-2:]}"
    try:
        r = _session.get(NBA_INJ_PAGE.format(s=saison), timeout=20)
        liens = re.findall(r'https?://[^"\']*Injury-Report_[^"\']*\.pdf', r.text)
        if not liens:
            return {}
        pdf = _session.get(liens[-1], timeout=30)
        if pdf.status_code != 200:
            return {}
        res = parser_rapport_officiel(texte_pdf(pdf.content))
        print(f"Rapport officiel NBA : {len(res)} joueurs ({liens[-1].rsplit('/', 1)[-1]})")
        return res
    except Exception as e:
        print("Rapport officiel indisponible :", e)
        return {}


def statut_joueur(aid, nom, inj_id, inj_nom, officiel):
    """Statut final : rapport officiel prioritaire, sinon ESPN. ('' = rien à signaler)."""
    n = norm_nom(nom)
    if n in officiel:
        return officiel[n]
    st, com = inj_id.get(aid) or inj_nom.get(n) or ("", "")
    if st.lower() == "day-to-day":
        c = com.lower()
        if any(x in c for x in ("will play", "expected to play", "probable", "will suit up", "available")):
            return "Probable"
        return "Questionable"
    return st


def exclu(statut):
    s = (statut or "").lower()
    return any(x in s for x in STATUTS_EXCLUS)


# ---------------------------------------------------------------- MATCHS DU SOIR + PROPS
def matchs_du_soir(maintenant_utc):
    """Matchs pas encore commencés dans la fenêtre, avec spread / total DK (ESPN)."""
    jour_et = maintenant_utc.astimezone(TZ_ET).date()
    res = []
    sb_global = None
    for j in (jour_et, jour_et + timedelta(days=1)):
        sb = scoreboard(j)
        sb_global = sb_global or sb
        for e in sb.get("events", []):
            c = e["competitions"][0]
            if c["status"]["type"].get("state") != "pre":
                continue
            debut = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
            if not (maintenant_utc - timedelta(minutes=5) <= debut <= maintenant_utc + timedelta(hours=FENETRE_HEURES)):
                continue
            eq = {t["homeAway"]: t["team"] for t in c["competitors"]}
            spread, total, fav = None, None, None
            for o in c.get("odds", []) or []:
                total = o.get("overUnder", total)
                det = o.get("details") or ""
                m = re.match(r"([A-Z]+)\s+(-?\d+(?:\.\d+)?)", det)
                if m:
                    fav, spread = m.group(1), abs(float(m.group(2)))
                elif det.upper() == "EVEN":
                    spread = 0.0
                break
            res.append({
                "id": e["id"], "debut": debut, "jour_et": j,
                "dom": {"id": eq["home"]["id"], "abr": eq["home"]["abbreviation"], "nom": eq["home"].get("displayName")},
                "ext": {"id": eq["away"]["id"], "abr": eq["away"]["abbreviation"], "nom": eq["away"].get("displayName")},
                "spread": spread, "total": total, "fav": fav,
            })
    debut_saison = None
    try:
        lg = sb_global["leagues"][0]
        cal = lg.get("calendar") or []
        debut_saison = datetime.fromisoformat(lg["season"]["startDate"].replace("Z", "+00:00")).date()
    except Exception:
        pass
    return res, debut_saison


def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def nettoyer_echelle(paliers, ligne=None, m="pts"):
    """Garde une échelle de paliers cohérente (match entier).
    ESPN mélange parfois sous le même nom une 2e échelle (1re mi-temps / quart-temps) :
    1) pour un même palier on garde la cote la plus basse (le match entier est toujours plus probable) ;
    2) on ancre l'échelle sur la ligne principale DK (over/under), sinon sur le plus haut palier
       coté entre 1.60 et 2.40 ; chaque palier doit rester cohérent (écart de log-cote <= 1.2)
       avec une loi centrée sur cette ligne, sinon il est retiré ;
    3) cotes croissantes avec le palier."""
    meilleur = {}
    for k, cote in paliers:
        if k not in meilleur or cote < meilleur[k]:
            meilleur[k] = cote
    if not meilleur:
        return {}
    if ligne is None:
        proches = [k for k, c in meilleur.items() if 1.60 <= c <= 2.40]
        ligne = (max(proches) - 0.5) if proches else None
    if ligne is not None:
        centre = ligne + 0.5
        garde = {}
        for k, cote in meilleur.items():
            if abs(logit(implicite(cote)) - logit(proba_au_moins(k, centre, DISPERSION_DEF[m]))) <= 1.2:
                garde[k] = cote
        meilleur = garde
    res, prec = {}, 0.0
    for k in sorted(meilleur):
        if meilleur[k] + 1e-9 >= prec:
            res[k] = meilleur[k]
            prec = meilleur[k]
    return res


def props_match(eid):
    """Paliers DraftKings du match (via ESPN). {athlete_id: {marche: {k: cote}}}."""
    brut, lignes = {}, {}
    page, pages = 1, 1
    while page <= pages and page <= 5:
        d = get_json(f"{ESPN_CORE}/events/{eid}/competitions/{eid}/odds/100/propBets",
                     {"lang": "en", "region": "us", "limit": 1000, "page": page})
        if not d:
            break
        pages = d.get("pageCount", 1) or 1
        for it in d.get("items", []):
            if "athlete" not in it:
                continue
            typ = it.get("type") or {}
            aid = ref_id(it["athlete"].get("$ref"), "athletes")
            cible = ((it.get("current") or {}).get("target") or {}).get("displayValue") \
                or ((it.get("odds") or {}).get("total") or {}).get("value") or ""
            cote = num(((it.get("odds") or {}).get("decimal") or {}).get("value"))
            if not aid or cote <= 1.0:
                continue
            m_ligne = TYPES_LIGNE.get(str(typ.get("id")))
            if m_ligne:
                lignes.setdefault(aid, {})[m_ligne] = num(cible, None)
                continue
            m = TYPES_PALIERS.get(str(typ.get("id"))) or MARCHES.get(typ.get("name"))
            mk = re.match(r"(\d+)\+", str(cible))
            if not m or not mk:
                continue
            brut.setdefault(aid, {}).setdefault(m, []).append((int(mk.group(1)), round(cote, 2)))
        page += 1
    res = {}
    for aid, marches in brut.items():
        for m, paliers in marches.items():
            ech = nettoyer_echelle(paliers, (lignes.get(aid) or {}).get(m), m)
            if ech:
                res.setdefault(aid, {})[m] = ech
    return res


# ---------------------------------------------------------------- MODÈLE JOUEUR
def profil_joueur(idx, aid):
    """Moyennes utiles d'un joueur (saison, 10 et 5 derniers), ou None."""
    l = idx["joueurs"].get(aid) or []
    if len(l) < MATCHS_MIN:
        return None
    rows = [x["row"] for x in l]
    mins = [r[3] for r in rows]

    def taux(sous, m):
        s_min = sum(r[3] for r in sous)
        return sum(val_stat(r, m) for r in sous) / s_min if s_min > 0 else 0.0
    mins5 = mins[-5:]
    std5 = (math.sqrt(sum((x - moyenne(mins5)) ** 2 for x in mins5) / len(mins5))
            if len(mins5) >= 2 else 0.0)
    p = {"n": len(rows), "l": l, "tid": l[-1]["tid"], "pos": rows[-1][2],
         "min_s": moyenne(mins), "min_10": moyenne(mins[-10:]), "min_5": moyenne(mins5),
         "min_vals5": mins5, "min_std_5": std5, "min_plancher_5": min(mins5) if mins5 else 0.0,
         "usage": moyenne([r[8] + 0.44 * r[9] + r[10] for r in rows[-10:]]),
         "titulaire": sum(r[11] for r in rows[-5:]) >= 3}
    for m in ("pts", "reb", "ast", "tpm", "pra"):
        vals = [val_stat(r, m) for r in rows]
        mu = moyenne(vals)
        var = moyenne([(v - mu) ** 2 for v in vals]) if len(vals) > 1 else mu
        p[m] = {"s": mu, "10": moyenne(vals[-10:]), "5": moyenne(vals[-5:]),
                "t_s": taux(rows, m), "t_10": taux(rows[-10:], m),
                "vals10": vals[-10:], "vals": vals,
                "disp_obs": (var / mu) if mu > 0 else 1.0}
        dom = [x["row"] for x in l if x["dom"]]
        ext = [x["row"] for x in l if not x["dom"]]
        p[m]["t_dom"], p[m]["n_dom"] = taux(dom, m), len(dom)
        p[m]["t_ext"], p[m]["n_ext"] = taux(ext, m), len(ext)
    return p



def matchs_manques(idx, pr, n=5):
    """Nombre de matchs de son équipe actuelle, parmi les n derniers, que le joueur a manqués."""
    team = idx["equipes"].get(pr["tid"], [])[-n:]
    joues = {x["gid"] for x in pr["l"]}
    return sum(1 for g in team if g["gid"] not in joues)


def commentaire_blessure(aid, nom, inj_id, inj_nom):
    pair = inj_id.get(aid) or inj_nom.get(norm_nom(nom))
    return (pair[1] if pair else "") or ""


def minutes_limitees(statut, commentaire=""):
    t = f"{statut or ''} {commentaire or ''}".lower()
    return any(m in t for m in MOTS_MINUTES_LIMITEES)


def favori_souffrant(c, seuil):
    """True si le joueur est dans l'équipe favorite avec un spread >= seuil."""
    sp, fav, eq = c.get("spread"), c.get("fav"), c.get("eq")
    return sp is not None and fav and eq and sp >= seuil and fav == eq


def raison_ecart(c, ticket):
    """Pourquoi écarter cette sélection du ticket, ou None si elle passe le filtre v1.2."""
    if minutes_limitees(c.get("statut"), c.get("commentaire")):
        return "minutes limitées / retour blessure"
    plancher = MINUTES_SOLIDE_SAFE if ticket in ("solide", "safe") else MINUTES_FUN
    if c.get("min_5") is not None and c["min_5"] < plancher:
        return f"minutes L5 {c.get('min_5', 0):.1f} < {plancher:g}"
    if c.get("min_plancher_5") is not None and c["min_plancher_5"] < MINUTES_PLANCHER_L5:
        return f"un match sous {MINUTES_PLANCHER_L5:g} min"
    if c.get("min_std_5") is not None and c["min_std_5"] > MINUTES_STD_MAX:
        return f"minutes instables (écart-type {c.get('min_std_5', 0):.1f} > {MINUTES_STD_MAX:g})"
    trous_max = TROUS_MAX_SOLIDE_SAFE if ticket in ("solide", "safe") else TROUS_MAX_FUN
    if c.get("trous") is not None and c["trous"] > trous_max:
        return f"retour d'absence ({c.get('trous')} matchs manqués)"
    if ticket in ("solide", "safe", "fun") and "titulaire" in c and not c["titulaire"]:
        return "pas titulaire (moins de 3 titularisations sur 5)"
    n10, hits = c.get("n10") or 0, c.get("hits10") or 0
    if ticket == "solide" and n10 >= 8 and hits < SOLIDES_HITS_MIN:
        return f"hit-rate L10 {hits}/{n10} < {SOLIDES_HITS_MIN}"
    if ticket == "safe" and n10 >= 8 and hits < SAFE_HITS_MIN:
        return f"hit-rate L10 {hits}/{n10} < {SAFE_HITS_MIN}"
    if ticket == "fun" and n10 >= 8 and hits < FUN_HITS_MIN:
        return f"hit-rate L10 {hits}/{n10} < {FUN_HITS_MIN}"
    if ticket == "safe" and c.get("m") == "tpm":
        return "marché 3-pts trop volatile pour le ticket safe"
    if ticket == "safe" and favori_souffrant(c, SAFE_BLOWOUT_SPREAD):
        return "favori à gros écart (risque blowout)"
    if ticket == "solide" and c.get("m") == "tpm" and favori_souffrant(c, SOLIDES_TPM_BLOWOUT):
        return "3-pts + favori à gros écart"
    if ticket == "fun" and c.get("m") == "tpm" and favori_souffrant(c, FUN_TPM_BLOWOUT):
        return "3-pts + favori à très gros écart"
    if ticket == "fun":
        notes = c.get("notes") or []
        if "back-to-back" in notes and "risque blowout" in notes:
            return "back-to-back + risque blowout"
    return None


def passe_filtre(c, ticket):
    return raison_ecart(c, ticket) is None


def resumer_filtres(cands):
    """Log des joueurs qui n'ont aucune sélection recevable sur un ticket."""
    par_joueur = {}
    for c in cands:
        par_joueur.setdefault(c["aid"], []).append(c)
    lignes = []
    comptes = {}
    for ticket, lib in (("solide", "solides"), ("safe", "safe"), ("fun", "fun")):
        n = 0
        for aid, cs in par_joueur.items():
            raisons = [raison_ecart(c, ticket) for c in cs]
            if any(r is None for r in raisons):
                continue
            # toutes les lignes échouent : on affiche une raison de rôle / minutes
            # (le hit-rate dépend du palier, donc il est moins parlant ici)
            priorite = ("minutes limitées", "absence", "titulaire", "sous ", "instables", "minutes L5",
                        "3-pts", "blowout", "hit-rate")
            elues = [r for r in raisons if r]
            r = next((x for prio in priorite for x in elues if prio in x), elues[0])
            n += 1
            c0 = cs[0]
            lignes.append(f"  · {c0['nom']} ({c0['eq']}) écarté ({lib}) : {r}")
        comptes[lib] = n
    print(f"Filtre v1.1 : {comptes['solides']} joueur(s) sans jambe solide, "
          f"{comptes['safe']} sans jambe safe, {comptes['fun']} sans jambe fun")
    for L in lignes:
        print(L)
    return lignes



def absents_equipe(idx, tid, actifs_statut, jour_et):
    """Coéquipiers importants absents ce soir ET qui jouaient encore récemment
    (s'ils manquent depuis longtemps, la forme récente l'intègre déjà)."""
    matchs = idx["equipes"].get(tid, [])[-5:]
    if not matchs:
        return []
    gids_recents = {x["gid"] for x in matchs}
    gids_2 = {x["gid"] for x in matchs[-2:]}
    res = []
    for aid, st in actifs_statut.items():
        if not st or not any(s in st.lower() for s in ("out", "doubtful", "suspen")):
            continue
        l = [x for x in idx["joueurs"].get(aid, []) if x["tid"] == tid]
        recents = [x for x in l if x["gid"] in gids_recents]
        if not recents or not any(x["gid"] in gids_2 for x in recents):
            continue
        if moyenne([x["row"][3] for x in recents]) < 20:
            continue
        rows = [x["row"] for x in l[-10:]]
        res.append({"aid": aid, "pts": moyenne([r[4] for r in rows]), "reb": moyenne([r[5] for r in rows]),
                    "ast": moyenne([r[6] for r in rows]), "tpm": moyenne([r[7] for r in rows]),
                    "usage": moyenne([r[8] + 0.44 * r[9] + r[10] for r in rows])})
    return res


def boost_absents(prof, m, absents, rotation):
    """Part des stats des absents récupérée par ce joueur (proportionnelle à son usage / sa stat)."""
    if not absents or not rotation:
        return 1.0
    sous = ["pts", "reb", "ast"] if m == "pra" else [m]
    gain, base = 0.0, 0.0
    for s in sous:
        manque = sum(a[s] for a in absents) * PART_REDISTRIBUEE
        if s in ("pts", "tpm"):
            tot = sum(r["usage"] for r in rotation) or 1
            part = prof["usage"] / tot
        else:
            tot = sum(r[s]["10"] for r in rotation) or 1
            part = prof[s]["10"] / tot
        gain += manque * part
        base += prof[s]["10"]
    if base <= 0:
        return 1.0
    return min(1 + gain / base, 1 + BOOST_ABSENTS_MAX)


def projeter(idx, prof, m, match, cote_dom, ctx):
    """Moyenne attendue ce soir + dispersion pour le marché m."""
    # minutes
    mp = 0.45 * prof["min_5"] + 0.30 * prof["min_10"] + 0.25 * prof["min_s"]
    tid = prof["tid"]
    adv = match["ext"]["id"] if cote_dom else match["dom"]["id"]
    notes = []
    if ctx["b2b"].get(tid):
        mp *= B2B_MINUTES
        notes.append("back-to-back")
    if match.get("spread") is not None and prof["min_10"] >= 28:
        for seuil, f in BLOWOUT:
            if match["spread"] >= seuil:
                mp *= f
                notes.append("risque blowout")
                break
    # taux par minute : forme 10 matchs + saison
    s = prof[m]
    w10 = 0.45 if prof["n"] >= 10 else 0.25
    taux = w10 * s["t_10"] + (1 - w10) * s["t_s"]
    mu = taux * mp
    # domicile / extérieur (lissé)
    t_lieu, n_lieu = (s["t_dom"], s["n_dom"]) if cote_dom else (s["t_ext"], s["n_ext"])
    if s["t_s"] > 0 and n_lieu:
        f = ((t_lieu * n_lieu + s["t_s"] * 10) / (n_lieu + 10)) / s["t_s"]
        mu *= min(max(f, BORNES_DOM[0]), BORNES_DOM[1])
    # défense adverse par poste
    fd = facteur_defense(idx, adv, prof["pos"], m)
    mu *= fd
    if fd >= 1.04:
        notes.append("défense adverse faible")
    elif fd <= 0.96:
        notes.append("défense adverse solide")
    # environnement du match : total DK (rythme + attaque) sinon pace calculé
    fe = 1.0
    p_eq, p_adv = idx["pace"].get(tid), idx["pace"].get(adv)
    f_pace = ((p_eq + p_adv) / 2 / idx["ligue_pace"]) if (p_eq and p_adv and idx["ligue_pace"]) else 1.0
    if m == "reb":
        fe = f_pace ** 0.5
    elif match.get("total") and match.get("spread") is not None:
        fav_dom = match.get("fav") == match["dom"]["abr"]
        sp = match["spread"]
        tot_eq = match["total"] / 2 + (sp / 2 if (fav_dom == cote_dom) else -sp / 2)
        fe = (tot_eq / idx["ligue_pts"]) ** 0.6 if idx["ligue_pts"] else 1.0
    else:
        fe = f_pace ** 0.7
    mu *= min(max(fe, BORNES_ENV[0]), BORNES_ENV[1])
    if ctx["b2b"].get(adv):
        mu *= B2B_ADV
    # coéquipiers absents
    fb = boost_absents(prof, m, ctx["absents"].get(tid, []), ctx["rotation"].get(tid, []))
    if fb > 1.03:
        notes.append("coéquipier(s) absent(s)")
    mu *= fb
    n = prof["n"]
    disp = (s["disp_obs"] * n + DISPERSION_DEF[m] * 10) / (n + 10)
    disp = min(max(disp, DISPERSION_BORNES[0]), DISPERSION_BORNES[1])
    return mu, disp, mp, notes


def implicite(cote):
    """Proba du book sans sa marge."""
    return min(1 / cote / (1 + MARGE_BOOK + (0.03 if cote >= 3 else 0)), 0.99)


def proba_finale(k, mu, disp, vals10, cote, estimee):
    p_nb = proba_au_moins(k, mu, disp)
    hits = sum(1 for v in vals10 if v >= k)
    p_emp = (hits + 3 * p_nb) / (len(vals10) + 3) if vals10 else p_nb
    p_mod = 0.75 * p_nb + 0.25 * p_emp
    p_mod = 0.5 + (p_mod - 0.5) * CALIBRATION        # calibré sur la saison (le brut était trop sûr de lui)
    if estimee or not cote:
        return p_mod, p_mod
    return POIDS_MODELE * p_mod + (1 - POIDS_MODELE) * implicite(cote), p_mod


def paliers_estimes(mu, disp, m):
    """Sans ligne DK : paliers type Stake (≈ 55-90 % et 25-50 %) avec cote estimée."""
    res = {}
    for k in range(max(1, PLANCHER_ESTIM[m]), int(mu * 2.2) + 2):
        p = proba_au_moins(k, mu, disp)
        if 0.25 <= p <= 0.90:
            res[k] = round(max(1.01, 1 / p / (1 + MARGE_ESTIM)), 2)
    return res


# ---------------------------------------------------------------- CANDIDATS
def candidats(idx, matchs, props, statuts, commentaires=None):
    ctx = {"b2b": {}, "absents": {}, "rotation": {}}
    profils = {}
    for mt in matchs:
        for cote_eq in ("dom", "ext"):
            tid = mt[cote_eq]["id"]
            ctx["b2b"][tid] = a_joue_la_veille(idx, tid, mt["jour_et"])
    # profils + statut de tous les joueurs récents des équipes du soir
    tids = {mt[c]["id"] for mt in matchs for c in ("dom", "ext")}
    effectif = {t: {} for t in tids}
    for aid, l in idx["joueurs"].items():
        tid = l[-1]["tid"]
        if tid in tids:
            effectif[tid][aid] = statuts.get(aid, "")
    for tid in tids:
        ctx["absents"][tid] = absents_equipe(idx, tid, effectif[tid], matchs[0]["jour_et"])
        rot = []
        for aid, st in effectif[tid].items():
            if exclu(st):
                continue
            pr = profil_joueur(idx, aid)
            if pr and pr["min_10"] >= 15 and pr["tid"] == tid:
                profils[aid] = pr
                rot.append(pr)
        ctx["rotation"][tid] = rot

    noms = idx["cache"]["noms"]
    commentaires = commentaires or {}
    res = []
    for mt in matchs:
        pm = props.get(mt["id"]) or {}
        estime_match = not pm
        for cote_eq in ("dom", "ext"):
            tid = mt[cote_eq]["id"]
            adv = mt["ext" if cote_eq == "dom" else "dom"]
            for aid, st in effectif[tid].items():
                if exclu(st):
                    continue
                pr = profils.get(aid)
                if not pr:
                    continue
                lignes = pm.get(aid)
                if not lignes and not estime_match:
                    continue                    # DK n'a pas de ligne : joueur pas assez sûr
                for m in ("pts", "reb", "ast", "pra", "tpm"):
                    mu, disp, mp, notes = projeter(idx, pr, m, mt, cote_eq == "dom", ctx)
                    if mp < MINUTES_MIN:
                        break
                    paliers = (lignes or {}).get(m)
                    estimee = paliers is None
                    if estimee:
                        if not estime_match:
                            continue
                        paliers = paliers_estimes(mu, disp, m)
                    for k, cote in paliers.items():
                        if cote < COTE_MIN:
                            continue
                        p, p_mod = proba_finale(k, mu, disp, pr[m]["vals10"], cote, estimee)
                        if not estimee and abs(p_mod - implicite(cote)) > ECART_MAX_MARCHE:
                            continue
                        res.append({
                            "eid": mt["id"], "aid": aid, "nom": noms.get(aid, [aid])[0],
                            "eq": mt[cote_eq]["abr"], "adv": adv["abr"], "dom": cote_eq == "dom",
                            "m": m, "k": int(k), "cote": cote, "estim": estimee,
                            "p": p, "p_mod": p_mod, "value": p * cote - 1, "mu": mu, "mp": mp,
                            "moy10": pr[m]["10"], "hits10": sum(1 for v in pr[m]["vals10"] if v >= k),
                            "n10": len(pr[m]["vals10"]), "notes": notes, "statut": st,
                            "commentaire": commentaires.get(aid, ""),
                            "debut": mt["debut"].isoformat(),
                            "titulaire": pr["titulaire"], "min_5": pr["min_5"],
                            "min_std_5": pr["min_std_5"], "min_plancher_5": pr["min_plancher_5"],
                            "trous": matchs_manques(idx, pr),
                            "spread": mt.get("spread"), "fav": mt.get("fav"),
                        })
    return res


# ---------------------------------------------------------------- TICKETS
def pronos_solides(cands):
    ok = [c for c in cands if c["p"] >= SOLIDES_PROBA_MIN and c["cote"] >= SOLIDES_COTE_MIN
          and c["value"] >= SOLIDES_VALUE_MIN and c["p_mod"] >= SOLIDES_PROBA_MIN
          and passe_filtre(c, "solide")]
    ok.sort(key=lambda c: (c["value"] * 0.6 + c["p"] * 0.4), reverse=True)
    choix, joueurs, par_match = [], set(), {}
    for c in ok:
        if c["aid"] in joueurs or par_match.get(c["eid"], 0) >= SOLIDES_MAX_PAR_MATCH:
            continue
        choix.append(c)
        joueurs.add(c["aid"])
        par_match[c["eid"]] = par_match.get(c["eid"], 0) + 1
        if len(choix) >= SOLIDES_MAX:
            break
    return choix


def ticket_safe(cands, exclure=()):
    ok = [c for c in cands if c["p"] >= SAFE_PROBA_MIN and c["p_mod"] >= SAFE_PROBA_MIN
          and COTE_MIN <= c["cote"] <= SAFE_COTE_MAX and passe_filtre(c, "safe")]
    ok.sort(key=lambda c: (c["p"], c["cote"]), reverse=True)
    choix, matchs, joueurs = [], set(), set()
    for c in ok:
        if c["eid"] in matchs or c["aid"] in joueurs:
            continue
        if len(choix) == 2 and c["p"] < SAFE_PROBA_3E:
            break
        choix.append(c)
        matchs.add(c["eid"])
        joueurs.add(c["aid"])
        if len(choix) >= SAFE_LEGS_MAX:
            break
    return choix if len(choix) >= 2 else []


def combine_fun(cands, exclure=(), prefer_exclure=()):
    """Combinaison la plus probable (1 sélection par match) qui atteint FUN_COTE_CIBLE.

    exclure : joueurs interdits (déjà sur le ticket safe).
    prefer_exclure : joueurs déjà en solides, écartés s'il reste une autre option dans le match.
    Si un match n'a aucune autre jambe recevable, on les garde (ticket plus long, pas de jambe faible).
    S'il n'y a pas assez de matchs solides pour viser 30, on rend le meilleur ticket propre (3+) ou rien.
    """
    interdits = set(exclure)
    ok = [c for c in cands if FUN_COTE_LEG[0] <= c["cote"] <= FUN_COTE_LEG[1]
          and c["p"] >= FUN_PROBA_LEG_MIN and c["value"] >= FUN_VALUE_MIN
          and c["aid"] not in interdits
          and passe_filtre(c, "fun")]
    par_match = {}
    # titulaires d'abord, puis proba ; plusieurs paliers d'un même marché restent possibles
    for c in sorted(ok, key=lambda c: (c.get("titulaire", False), c["p"]), reverse=True):
        l = par_match.setdefault(c["eid"], [])
        cle = (c["aid"], c.get("m"), c.get("k"), c["cote"])
        if len(l) < FUN_CHOIX_PAR_MATCH and all(
                (x["aid"], x.get("m"), x.get("k"), x["cote"]) != cle for x in l):
            l.append(c)
    eviter = set(prefer_exclure)
    for eid, options in list(par_match.items()):
        sans = [c for c in options if c["aid"] not in eviter]
        par_match[eid] = sans if sans else options
        if not par_match[eid]:
            del par_match[eid]
    cible = math.log(FUN_COTE_CIBLE)
    pas = 0.04
    # état : (nb, case_cote) -> (log_p, log_cote, legs)
    etats = {(0, 0): (0.0, 0.0, [])}
    nmax = max(FUN_LEGS)
    for eid, options in par_match.items():
        nouveaux = dict(etats)
        for (n, b), (lp, lc, legs) in etats.items():
            if n >= nmax:
                continue
            for c in options:
                nlc = lc + math.log(c["cote"])
                cle = (n + 1, min(int(nlc / pas), int(cible / pas) + 1))
                nlp = lp + math.log(c["p"])
                if cle not in nouveaux or nouveaux[cle][0] < nlp:
                    nouveaux[cle] = (nlp, nlc, legs + [c])
        etats = nouveaux
    n_matchs = len(par_match)
    if n_matchs >= min(FUN_LEGS):
        tailles = {n for n in FUN_LEGS if n <= n_matchs}
    elif n_matchs >= FUN_LEGS_REPLI:
        tailles = {n_matchs}          # ticket plus court, la cible 30 ne sera en général pas atteinte
    else:
        tailles = set()               # pas assez de matchs propres : on ne comble pas avec une jambe faible
    meilleur, repli = None, None
    for (n, b), (lp, lc, legs) in etats.items():
        if n not in tailles:
            continue
        if lc >= cible - 1e-9:
            if meilleur is None or lp > meilleur[0]:
                meilleur = (lp, lc, legs)
        elif repli is None or lc > repli[1]:
            repli = (lp, lc, legs)
    choix = meilleur or repli
    if not choix:
        return [], False
    legs = sorted(choix[2], key=lambda c: c["debut"])
    return legs, meilleur is not None


def cote_totale(legs):
    return math.prod(c["cote"] for c in legs)


def chance_totale(legs):
    return math.prod(c["p"] for c in legs)


# ---------------------------------------------------------------- MESSAGES
def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def libelle(c, court=False):
    seuil = f"{c['k'] - 0.5:g}"
    if court:
        return f"{seuil}+ {COURT_MARCHE[c['m']]}"
    return f"Plus de {seuil} {NOM_MARCHE[c['m']]}"


def fmt_cote(c):
    return f"{c['cote']:.2f}" + (" (estim.)" if c["estim"] else "")


def heure_paris(iso):
    return datetime.fromisoformat(iso).astimezone(TZ_PARIS).strftime("%Hh%M")


def bloc_leg(i, c, numero=True):
    puce = (f"{i}\ufe0f\u20e3" if i < 10 else f"{i}.") if numero else "▫️"
    lieu = "vs" if c["dom"] else "@"
    l1 = f"{puce} <b>{esc(c['nom'])}</b> ({c['eq']}) · {EMOJI_MARCHE[c['m']]} {libelle(c)}"
    l2 = (f"   {lieu} {c['adv']} · {heure_paris(c['debut'])} · 📊 {round(c['p'] * 100)} % · 💰 {fmt_cote(c)}")
    l3 = f"   📈 moy. 10 derniers : {c['moy10']:.1f} · {c['hits10']}/{c['n10']} au-dessus"
    if c["notes"]:
        l3 += " · " + ", ".join(c["notes"][:2])
    return "\n".join([l1, l2, l3])


def titre_date(jour_et):
    d = jour_et
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month]}"


def message_principal(jour_et, nb_matchs, solides, fun, fun_ok, safe, bilan, sources):
    L = [f"🏀 <b>NBA · PROPS JOUEURS · NUIT DU {titre_date(jour_et)}</b>",
         f"<i>{nb_matchs} match(s) cette nuit</i>"]
    if bilan:
        L += ["", bilan]
    L += ["", "🏀 <b>PRONOS SOLIDES DU SOIR</b>"]
    if solides:
        for c in solides:
            L += ["", bloc_leg(0, c, numero=False)]
    else:
        L += ["Aucun prono assez solide ce soir (pas de forcing)."]
    L += ["", f"🎉 <b>COMBINÉ FUN</b> ({len(fun)} sélections)" if fun else "🎉 <b>COMBINÉ FUN</b>"]
    if fun:
        for i, c in enumerate(fun, 1):
            L += [bloc_leg(i, c)]
        L += ["", f"💰 Cote totale : <b>{cote_totale(fun):.2f}</b> · 📊 chance estimée ≈ {chance_totale(fun) * 100:.1f} %"]
        if not fun_ok:
            L += [f"<i>Pas de combinaison cohérente à {FUN_COTE_CIBLE:g}+ ce soir : voici la meilleure possible "
                  f"(pas de jambe faible juste pour gonfler la cote).</i>"]
        L += ["<i>Ticket fun : petite mise.</i>"]
    else:
        L += ["Pas assez de matchs / sélections pour un combiné cohérent."]
    L += ["", f"🛡️ <b>TICKET SAFE</b> ({len(safe)} sélections)" if safe else "🛡️ <b>TICKET SAFE</b>"]
    if safe:
        for i, c in enumerate(safe, 1):
            L += [bloc_leg(i, c)]
        L += ["", f"💰 Cote totale : <b>{cote_totale(safe):.2f}</b> · 📊 chance estimée ≈ {chance_totale(safe) * 100:.0f} %"]
    else:
        L += ["Pas de ticket safe ce soir (aucune sélection assez sûre à 1.20 ou plus)."]
    if any(c["estim"] for c in solides + fun + safe):
        L += ["", "⚠️ (estim.) : pas de ligne réelle trouvée, cote du modèle, vérifie-la sur Stake."]
    L += ["", f"<i>Cotes : DraftKings ({sources}). Stake peut différer un peu : vérifie la ligne avant de miser. "
              "Blessures vérifiées sur le rapport officiel NBA.</i>",
          "", "🔞 Jouer comporte des risques : endettement, isolement, dépendance. "
              "Aide : 09 74 75 13 13 (appel non surtaxé). Aucun gain garanti."]
    return "\n".join(L)


def longueur_x(texte):
    """Longueur façon X : emojis et caractères larges comptent pour 2."""
    n = 0
    for ch in texte:
        o = ord(ch)
        if o in (0xFE0F, 0x200D, 0x20E3):
            continue
        n += 2 if o > 0x2000 else 1
    return n


def texte_x(fun, fun_ok):
    if not fun:
        return None
    total = cote_totale(fun)

    def construit(nom_court):
        lignes = []
        for c in fun:
            nom = c["nom"].split(" ", 1)[-1] if nom_court else c["nom"]
            lignes.append(f"✅ {nom} {libelle(c, court=True)}")
        return ("🏀 COMBINÉ NBA DE LA NUIT 🎉\n\n" + "\n".join(lignes) +
                f"\n\n💰 Cote : {total:.2f}\n🔥 Qui suit ? #NBA #PariSportif #NBAProps")
    for court in (False, True):
        t = construit(court)
        if longueur_x(t) <= 280:
            return t
    t = construit(True).replace(" #NBAProps", "").replace("🔥 Qui suit ? ", "")
    return t if longueur_x(t) <= 280 else t[:270]


# ---------------------------------------------------------------- TELEGRAM
def envoyer_telegram(textes):
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("CHAT_ID") or "").strip()
    if not token or not chat or os.environ.get("NBA_DRY_RUN"):
        for t in textes:
            print(t)
            print("-" * 40)
        return
    for t in textes:
        for essai in range(5):
            try:
                r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                  data={"chat_id": chat, "text": t[:4090], "parse_mode": "HTML",
                                        "disable_web_page_preview": "true"}, timeout=(10, 20))
                print("Telegram:", r.status_code)
                if r.status_code == 200 or 400 <= r.status_code < 429:
                    if r.status_code != 200:
                        print(r.text[:300])
                    break
            except Exception as e:
                print("Telegram échec :", e)
            time.sleep(10 * (essai + 1))
        time.sleep(1)


def couper(texte, limite=3900):
    """Découpe un long message sur les lignes vides (limite Telegram 4096)."""
    if len(texte) <= limite:
        return [texte]
    morceaux, cour = [], ""
    for bloc in texte.split("\n\n"):
        if len(cour) + len(bloc) + 2 > limite and cour:
            morceaux.append(cour)
            cour = bloc
        else:
            cour = bloc if not cour else cour + "\n\n" + bloc
    if cour:
        morceaux.append(cour)
    return morceaux


# ---------------------------------------------------------------- HISTORIQUE / BILAN
def charger_historique():
    if not os.path.exists(HISTORIQUE):
        return {"picks": [], "tickets": []}
    try:
        with open(HISTORIQUE, encoding="utf-8") as f:
            h = json.load(f)
        h.setdefault("picks", [])
        h.setdefault("tickets", [])
        return h
    except Exception as e:
        print("Historique illisible, suivi désactivé :", e)
        return None


def sauver_historique(h):
    with open(HISTORIQUE, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=1)


def pick_dict(c, jour, ticket):
    return {"date": jour, "ticket": ticket, "eid": c["eid"], "aid": c["aid"], "nom": c["nom"],
            "eq": c["eq"], "m": c["m"], "k": c["k"], "cote": c["cote"], "estim": c["estim"],
            "p": round(c["p"], 3), "res": None}


def journaliser(h, jour, solides, fun, safe):
    """Remplace les entrées du même jour (une relance n'ajoute pas de doublon)."""
    h["picks"] = [p for p in h["picks"] if p["date"] != jour]
    h["tickets"] = [t for t in h["tickets"] if t["date"] != jour]
    for c in solides:
        h["picks"].append(pick_dict(c, jour, "solide"))
    for nom, legs in (("fun", fun), ("safe", safe)):
        if legs:
            h["tickets"].append({"date": jour, "type": nom, "cote": round(cote_totale(legs), 2),
                                 "legs": [pick_dict(c, jour, nom) for c in legs], "res": None})


def stat_box(cache, eid, aid, m):
    """Valeur réelle de la stat ; 'dnp' si le joueur n'a pas joué ; None si match pas fini."""
    g = cache["games"].get(eid)
    if not g:
        return None
    for row in g["p"]:
        if row[0] == aid:
            return val_stat(row, m)
    return "dnp"


def regler(h, cache, aujourdhui):
    limite = (aujourdhui - timedelta(days=4)).isoformat()

    def regle_pick(p):
        if p.get("res"):
            return
        v = stat_box(cache, p["eid"], p["aid"], p["m"])
        if v is None:
            if p["date"] < limite:
                p["res"] = "annule"
            return
        p["val"] = None if v == "dnp" else v
        p["res"] = "annule" if v == "dnp" else ("gagne" if v >= p["k"] else "perdu")
    for p in h["picks"]:
        regle_pick(p)
    for t in h["tickets"]:
        for l in t["legs"]:
            regle_pick(l)
        if t.get("res"):
            continue
        res = [l["res"] for l in t["legs"]]
        if "perdu" in res:
            t["res"] = "perdu"
        elif all(r for r in res):
            valides = [l for l in t["legs"] if l["res"] == "gagne"]
            t["res"] = "gagne" if valides else "annule"
            t["cote_reelle"] = round(math.prod(l["cote"] for l in valides), 2) if valides else 1.0


def faire_bilan(h, aujourdhui):
    debut = (aujourdhui - timedelta(days=BILAN_JOURS)).isoformat()
    veille = [p for p in h["picks"] if p["date"] < aujourdhui.isoformat()]
    if not veille and not h["tickets"]:
        return None
    pk = [p for p in h["picks"] if p["date"] >= debut and p.get("res") in ("gagne", "perdu")]
    lignes = [f"📊 <b>BILAN {BILAN_JOURS} JOURS</b>"]
    if pk:
        g = sum(1 for p in pk if p["res"] == "gagne")
        roi = sum((p["cote"] - 1) if p["res"] == "gagne" else -1 for p in pk)
        lignes.append(f"🏀 Solides : {g}/{len(pk)} ({g / len(pk) * 100:.0f} %) · {roi:+.1f} u")
    for typ, nom in (("fun", "🎉 Fun"), ("safe", "🛡️ Safe")):
        ts = [t for t in h["tickets"] if t["type"] == typ and t["date"] >= debut and t.get("res") in ("gagne", "perdu")]
        if ts:
            g = sum(1 for t in ts if t["res"] == "gagne")
            roi = sum((t.get("cote_reelle", t["cote"]) - 1) if t["res"] == "gagne" else -1 for t in ts)
            lignes.append(f"{nom} : {g}/{len(ts)} · {roi:+.1f} u")
    # détail d'hier
    dates = sorted({p["date"] for p in h["picks"] if p.get("res")} |
                   {t["date"] for t in h["tickets"] if t.get("res")})
    if dates:
        d = dates[-1]
        det = []
        for p in [p for p in h["picks"] if p["date"] == d]:
            if p.get("res") in ("gagne", "perdu"):
                det.append(f"{'✅' if p['res'] == 'gagne' else '❌'} {esc(p['nom'])} {p['k'] - 0.5:g}+ "
                           f"{COURT_MARCHE[p['m']]} ({p.get('val')})")
        for t in [t for t in h["tickets"] if t["date"] == d and t.get("res")]:
            ic = {"gagne": "✅", "perdu": "❌"}.get(t["res"], "➖")
            det.append(f"{ic} Ticket {t['type']} @ {t['cote']:.2f}")
        if det:
            lignes.append(f"<i>Dernière nuit ({d}) :</i>")
            lignes += det[:10]
    return "\n".join(lignes) if len(lignes) > 1 else None


# ---------------------------------------------------------------- MAIN
def main():
    t0 = time.time()
    maintenant = datetime.now(timezone.utc)
    jour_et = maintenant.astimezone(TZ_ET).date()
    matchs, debut_saison = matchs_du_soir(maintenant)
    print(f"Matchs NBA dans les {FENETRE_HEURES} h : {len(matchs)}")
    if debut_saison is None:
        debut_saison = datetime(jour_et.year if jour_et.month >= 8 else jour_et.year - 1, 10, 1).date()

    cache = charger_cache()
    cache = maj_cache(cache, jour_et, debut_saison)
    sauver_cache(cache)
    idx = construire_index(cache)
    print(f"Box scores en cache : {len(cache['games'])} matchs · pace ligue {idx['ligue_pace']:.1f}")

    historique = charger_historique()
    bilan = None
    if historique is not None:
        regler(historique, cache, jour_et)
        bilan = faire_bilan(historique, jour_et)

    if not matchs:
        msgs = []
        if bilan:
            msgs.append(bilan)
        msgs.append("🏀 <b>NBA</b> : pas de match cette nuit. Repos 😴")
        envoyer_telegram(msgs)
        if historique is not None:
            sauver_historique(historique)
        return

    with ThreadPoolExecutor(WORKERS) as ex:
        props = dict(zip([m["id"] for m in matchs], ex.map(props_match, [m["id"] for m in matchs])))
    nb_lignes = sum(len(v) for p in props.values() for v in p.values())
    print("Props DK :", {m["ext"]["abr"] + "@" + m["dom"]["abr"]: len(props[m["id"]]) for m in matchs},
          f"({nb_lignes} marchés joueur)")

    inj_nom, inj_id = blessures_espn()
    officiel = blessures_officielles(maintenant.astimezone(TZ_ET))
    statuts, commentaires = {}, {}
    for aid, info in cache["noms"].items():
        st = statut_joueur(aid, info[0], inj_id, inj_nom, officiel)
        if st:
            statuts[aid] = st
        com = commentaire_blessure(aid, info[0], inj_id, inj_nom)
        if com:
            commentaires[aid] = com
    print(f"Blessures : ESPN {len(inj_id)} · officiel {len(officiel)} · signalés {len(statuts)}")

    cands = candidats(idx, matchs, props, statuts, commentaires)
    print(f"Sélections possibles : {len(cands)}")
    if cands:
        resumer_filtres(cands)
    solides = pronos_solides(cands)
    safe = ticket_safe(cands)
    fun, fun_ok = combine_fun(cands,
                              exclure={c["aid"] for c in safe},
                              prefer_exclure={c["aid"] for c in solides})

    sources = "ESPN, gratuit" if any(props.values()) else "aucune ligne trouvée"
    msg = message_principal(matchs[0]["jour_et"], len(matchs), solides, fun, fun_ok, safe, bilan, sources)
    textes = couper(msg)
    tx = texte_x(fun, fun_ok)
    if tx:
        textes.append("📲 <b>TEXTE X</b> (appuie dessus pour copier)\n\n<code>" + esc(tx) + "</code>")
    envoyer_telegram(textes)

    if historique is not None:
        journaliser(historique, matchs[0]["jour_et"].isoformat(), solides, fun, safe)
        sauver_historique(historique)
    print(f"Terminé en {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()

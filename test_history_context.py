import unittest

from Nba_bot import journaliser, pick_dict


class HistoryContextTests(unittest.TestCase):
    def test_minutes_injury_opponent_and_absences_are_persisted(self):
        candidate = {
            "eid": "event-1",
            "aid": "player-1",
            "nom": "Test Player",
            "eq": "HOME",
            "m": "pts",
            "k": 20,
            "cote": 1.8,
            "estim": False,
            "p": 0.6824,
            "p_mod": 0.6617,
            "mu": 22.456,
            "mp": 34.56,
            "min_5": 32.24,
            "min_std_5": 3.15,
            "min_plancher_5": 27.0,
            "titulaire": True,
            "adv": "BOS",
            "adv_id": "team-2",
            "statut": "Questionable",
            "commentaire": "Ankle soreness",
            "absents_coequipiers": [
                {"aid": "player-2", "nom": "Teammate", "statut": "Out", "commentaire": "Knee"}
            ],
            "notes": ["coéquipier(s) absent(s)"],
        }
        history = {"picks": [], "tickets": []}

        journaliser(history, "2026-11-28", [candidate], [candidate], [candidate])

        records = [history["picks"][0]] + [ticket["legs"][0] for ticket in history["tickets"]]
        self.assertEqual(len(records), 3)
        for record in records:
            self.assertEqual(record["minutes_projetees"], 34.6)
            self.assertEqual(record["minutes_l5"], 32.2)
            self.assertEqual(record["adversaire"], "BOS")
            self.assertEqual(record["adversaire_id"], "team-2")
            self.assertEqual(record["statut_blessure"], "Questionable")
            self.assertEqual(record["commentaire_blessure"], "Ankle soreness")
            self.assertEqual(record["absents_coequipiers"][0]["nom"], "Teammate")
            self.assertEqual(record["projection_moyenne"], 22.46)
            self.assertEqual(record["proba_modele"], 0.662)


    def test_legacy_candidate_without_context_fields_is_still_supported(self):
        candidate = {
            "eid": "event-legacy",
            "aid": "player-legacy",
            "nom": "Legacy Player",
            "eq": "HOME",
            "m": "pts",
            "k": 15,
            "cote": 1.7,
            "estim": False,
            "p": 0.6,
        }

        record = pick_dict(candidate, "2026-11-28", None)

        self.assertIsNone(record["minutes_projetees"])
        self.assertIsNone(record["adversaire"])
        self.assertIsNone(record["statut_blessure"])
        self.assertEqual(record["absents_coequipiers"], [])
        self.assertEqual(record["notes_projection"], [])


if __name__ == "__main__":
    unittest.main()

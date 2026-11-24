import unittest

from backtest import bilan


class BilanTests(unittest.TestCase):
    def test_empty_sample_has_no_report(self):
        self.assertIsNone(bilan([]))

    def test_winning_pick_earns_odds_minus_stake(self):
        result = bilan([{"res": "gagne", "cote": 2.5, "p": 0.6}])

        self.assertEqual(result["profit"], 1.5)
        self.assertEqual(result["roi"], 1.5)

    def test_losing_pick_loses_one_unit(self):
        result = bilan([{"res": "perdu", "cote": 2.5, "p": 0.4}])

        self.assertEqual(result["profit"], -1.0)
        self.assertEqual(result["roi"], -1.0)

    def test_mixed_sample_subtracts_stake_only_for_losses(self):
        picks = [
            {"res": "gagne", "cote": 2.5, "p": 0.6},
            {"res": "perdu", "cote": 1.8, "p": 0.5},
        ]

        result = bilan(picks)

        self.assertEqual(result["profit"], 0.5)
        self.assertEqual(result["roi"], 0.25)


if __name__ == "__main__":
    unittest.main()

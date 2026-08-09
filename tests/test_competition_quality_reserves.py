import unittest

from engine.competition_quality import _looks_like_reserve_team


class ReserveTeamDetectionTests(unittest.TestCase):
    def test_common_second_team_suffixes_are_excluded(self):
        for name in (
            "Zamora FC B",
            "Puerto Cabello II",
            "New England II",
            "Austin II",
            "Sporting KC II",
            "Barcelona B",
            "Bayern Munich II",
        ):
            with self.subTest(name=name):
                self.assertTrue(_looks_like_reserve_team(name))

    def test_normal_senior_names_are_not_excluded(self):
        for name in (
            "O'Higgins",
            "Deportes Limache",
            "Santos",
            "Athletico Paranaense",
            "Orense SC",
            "Colo Colo",
        ):
            with self.subTest(name=name):
                self.assertFalse(_looks_like_reserve_team(name))


if __name__ == "__main__":
    unittest.main()

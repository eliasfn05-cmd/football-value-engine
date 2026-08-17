from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from engine.batch_features import BatchFeatureEngineeringService


class _CursorContext:
    def __init__(self, rows):
        self.cursor = Mock()
        self.cursor.fetchall.return_value = rows

    def __enter__(self):
        return self.cursor

    def __exit__(self, exc_type, exc, tb):
        return False


class BatchFeaturePostgresFastPathTests(SimpleTestCase):
    def test_history_fast_path_uses_one_query_without_secondary_fetch(self):
        kickoff = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
        requested = [
            SimpleNamespace(id=1, home_team_id=10, away_team_id=20, kickoff=kickoff),
            SimpleNamespace(id=2, home_team_id=30, away_team_id=40, kickoff=kickoff),
        ]
        service = BatchFeatureEngineeringService(requested, venue_sample_size=5)

        rows = [
            (10, "home", 101, 10, 99, 2, 1, kickoff),
            (10, "home", 102, 10, 98, 1, 1, kickoff),
            (20, "away", 102, 10, 20, 1, 1, kickoff),
        ]
        cursor_ctx = _CursorContext(rows)

        fake_connection = SimpleNamespace(
            vendor="postgresql",
            ops=SimpleNamespace(quote_name=lambda name: f'"{name}"'),
            cursor=lambda: cursor_ctx,
        )
        fake_fixture_model = SimpleNamespace(
            _meta=SimpleNamespace(db_table="engine_fixture"),
        )

        with patch("engine.batch_features.connection", fake_connection), patch(
            "engine.batch_features.Fixture", fake_fixture_model
        ):
            service._preload_history_postgres()

        self.assertEqual(cursor_ctx.cursor.execute.call_count, 1)
        sql, params = cursor_ctx.cursor.execute.call_args.args
        self.assertEqual(sql.count("CROSS JOIN LATERAL"), 2)
        self.assertIn("ORDER BY f.kickoff DESC", sql)
        self.assertEqual(params[2], 5)
        self.assertEqual(params[4], 5)
        self.assertEqual([row.id for row in service._history[(10, "home")]], [101, 102])
        self.assertEqual(service._history[(10, "home")][0].home_goals, 2)
        self.assertEqual(service._history[(20, "away")][0].away_goals, 1)

    def test_odds_fast_path_uses_two_indexed_lateral_probes_per_fixture(self):
        kickoff = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
        requested = [
            SimpleNamespace(id=1, home_team_id=10, away_team_id=20, kickoff=kickoff),
            SimpleNamespace(id=2, home_team_id=30, away_team_id=40, kickoff=kickoff),
        ]
        service = BatchFeatureEngineeringService(requested)
        rows = [
            (1, "BTTS", "YES", 1.82),
            (1, "OVER_2_5", "OVER", 1.75),
            (2, "OVER_2_5", "OVER", 1.91),
        ]
        cursor_ctx = _CursorContext(rows)
        fake_connection = SimpleNamespace(
            vendor="postgresql",
            ops=SimpleNamespace(quote_name=lambda name: f'"{name}"'),
            cursor=lambda: cursor_ctx,
        )
        fake_odds_model = SimpleNamespace(
            _meta=SimpleNamespace(db_table="engine_oddssnapshot"),
        )

        with patch("engine.batch_features.connection", fake_connection), patch(
            "engine.batch_features.OddsSnapshot", fake_odds_model
        ):
            service._preload_odds_postgres()

        self.assertEqual(cursor_ctx.cursor.execute.call_count, 1)
        sql, params = cursor_ctx.cursor.execute.call_args.args
        self.assertEqual(sql.count("CROSS JOIN LATERAL"), 2)
        self.assertIn("ORDER BY o.captured_at DESC", sql)
        self.assertEqual(params, [[1, 2]])
        self.assertEqual(service._odds[(1, "BTTS", "YES")], 1.82)
        self.assertEqual(service._odds[(1, "OVER_2_5", "OVER")], 1.75)
        self.assertEqual(service._odds[(2, "OVER_2_5", "OVER")], 1.91)

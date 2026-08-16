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
    def test_history_fast_path_uses_one_lateral_query_and_one_bulk_fetch(self):
        kickoff = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
        requested = [
            SimpleNamespace(id=1, home_team_id=10, away_team_id=20, kickoff=kickoff),
            SimpleNamespace(id=2, home_team_id=30, away_team_id=40, kickoff=kickoff),
        ]
        service = BatchFeatureEngineeringService(requested, venue_sample_size=5)

        hist_101 = SimpleNamespace(id=101, home_goals=2, away_goals=1)
        hist_102 = SimpleNamespace(id=102, home_goals=1, away_goals=1)
        rows = [
            (10, "home", 101, kickoff),
            (10, "home", 102, kickoff),
            (20, "away", 102, kickoff),
        ]
        cursor_ctx = _CursorContext(rows)

        fake_connection = SimpleNamespace(
            vendor="postgresql",
            ops=SimpleNamespace(quote_name=lambda name: f'"{name}"'),
            cursor=lambda: cursor_ctx,
        )
        fake_manager = Mock()
        fake_manager.only.return_value.in_bulk.return_value = {
            101: hist_101,
            102: hist_102,
        }
        fake_fixture_model = SimpleNamespace(
            _meta=SimpleNamespace(db_table="engine_fixture"),
            objects=fake_manager,
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
        fake_manager.only.return_value.in_bulk.assert_called_once()
        self.assertEqual(service._history[(10, "home")], [hist_101, hist_102])
        self.assertEqual(service._history[(20, "away")], [hist_102])

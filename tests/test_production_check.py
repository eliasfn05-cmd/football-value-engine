from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.test import TestCase


class ProductionCheckCommandTests(TestCase):
    def test_offline_preflight_passes_with_database(self):
        stdout = StringIO()
        call_command("production_check", "--skip-provider", stdout=stdout)
        output = stdout.getvalue()
        self.assertIn('"database"', output)
        self.assertIn('"ok": true', output.lower())
        self.assertIn("Production preflight passed", output)

import json

from django.core.management.base import BaseCommand

from backtesting.services import SettlementService


class Command(BaseCommand):
    help = "Settle predictions whose fixtures already have final scores."

    def add_arguments(self, parser):
        parser.add_argument("--model-version", dest="model_version")

    def handle(self, *args, **options):
        result = SettlementService().settle_finished(model_version=options.get("model_version"))
        self.stdout.write(json.dumps(result, indent=2, ensure_ascii=False))

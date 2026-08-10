import json

from django.core.management.base import BaseCommand

from backtesting.premium_settlement import settle_published_premium
from backtesting.services import SettlementService


class Command(BaseCommand):
    help = "Settle official Premium picks whose fixtures already have final scores."

    def add_arguments(self, parser):
        parser.add_argument("--model-version", dest="model_version")
        parser.add_argument(
            "--all-predictions",
            action="store_true",
            help="Compatibility/debug mode: settle every prediction, not only official Premium publications.",
        )

    def handle(self, *args, **options):
        if options.get("all_predictions"):
            result = SettlementService().settle_finished(model_version=options.get("model_version"))
        else:
            result = settle_published_premium(model_version=options.get("model_version"))
        self.stdout.write(json.dumps(result, indent=2, ensure_ascii=False))

import json

from django.core.management.base import BaseCommand

from backtesting.services import LearningAnalyticsService
from engine.score_v8 import V8_MODEL_VERSION


class Command(BaseCommand):
    help = "Report settled performance by model, market, tier and activated rules."

    def add_arguments(self, parser):
        parser.add_argument("--model-version", default=V8_MODEL_VERSION)
        parser.add_argument("--premium-only", action="store_true")
        parser.add_argument("--persist", action="store_true")

    def handle(self, *args, **options):
        service = LearningAnalyticsService()
        model_version = options["model_version"]
        premium_only = options["premium_only"]

        if options["persist"]:
            snapshots = service.persist_report(model_version=model_version, premium_only=premium_only)
            payload = {"model_version": model_version, "premium_only": premium_only, "snapshots_created": len(snapshots)}
        else:
            summaries = service.report(model_version=model_version, premium_only=premium_only)
            payload = {
                "model_version": model_version,
                "premium_only": premium_only,
                "scopes": [item.to_dict() for item in summaries],
            }
        self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False, default=str))

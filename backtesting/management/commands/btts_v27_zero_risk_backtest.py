from django.core.management.base import BaseCommand
from django.db.models import Q

from engine.btts_v27_policy import anti_zero_decision_v27, tier_a_decision_v27
from engine.models import Fixture, Prediction


CASES = [
    ("Bremer", "Phonix Lubeck"),
    ("Tampa Bay Rowdies", "Louisville City"),
    ("Fluminense", "Rivadavia"),
    ("QPR", "Bolton"),
]


class Command(BaseCommand):
    help = "Retro-audita los 0-0 historicos que motivaron BTTS V2.7."

    def _fixture(self, home, away):
        return (
            Fixture.objects.filter(
                Q(home_team__name__icontains=home) | Q(away_team__name__icontains=home),
                Q(home_team__name__icontains=away) | Q(away_team__name__icontains=away),
            )
            .select_related("home_team", "away_team")
            .order_by("-kickoff")
            .first()
        )

    def handle(self, *args, **options):
        rejected = 0
        evaluated = 0
        for home, away in CASES:
            fixture = self._fixture(home, away)
            if fixture is None:
                self.stdout.write(self.style.WARNING(f"NO DATA: {home} vs {away}"))
                continue

            prediction = (
                Prediction.objects.filter(fixture=fixture, market__iexact="BTTS")
                .order_by("-created_at")
                .first()
            )
            if prediction is None:
                self.stdout.write(self.style.WARNING(f"NO BTTS PREDICTION: {fixture}"))
                continue

            evaluated += 1
            generic = anti_zero_decision_v27(prediction)
            tier_a = tier_a_decision_v27(prediction)
            blocked = bool((generic and generic.blocked) or (tier_a and tier_a.blocked))
            reason = (
                (tier_a.code if tier_a and tier_a.blocked else None)
                or (generic.code if generic and generic.blocked else None)
                or "passes_v27"
            )
            if blocked:
                rejected += 1
                self.stdout.write(self.style.SUCCESS(f"REJECTED: {fixture} -> {reason}"))
            else:
                self.stdout.write(self.style.ERROR(f"STILL PASSES: {fixture}"))

        self.stdout.write(f"BTTS V2.7 zero-risk regression: rejected={rejected}/{evaluated} evaluated cases")
        if evaluated and rejected < evaluated:
            self.stdout.write(self.style.WARNING("Review any STILL PASSES case before promoting V2.7 thresholds further."))

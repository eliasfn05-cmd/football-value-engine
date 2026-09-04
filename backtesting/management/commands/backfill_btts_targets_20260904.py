from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import BaseCommand

from backtesting.management.commands.btts_filter_20260904 import DAY, TARGETS, sim
from engine.competition_quality import classify_competition
from engine.models import Fixture, FixtureScoreState, Prediction
from engine.score_v8 import V8_MODEL_VERSION


class Command(BaseCommand):
    help = "Backfill only the Sep 4 screenshot fixtures that are missing a V8 BTTS prediction."

    def handle(self, *args, **options):
        fixtures = list(
            Fixture.objects.filter(kickoff__date=DAY)
            .select_related("home_team", "away_team", "competition_ref")
            .order_by("kickoff")
        )

        matched = []
        missing_targets = 0
        excluded = 0
        already_predicted = 0
        stale_cleared = 0
        created = 0
        still_missing = []

        seen_fixture_ids = set()
        for home, away in TARGETS:
            scored = sorted(
                [
                    ((sim(home, f.home_team.name) + sim(away, f.away_team.name)) / 2.0, f)
                    for f in fixtures
                ],
                key=lambda item: item[0],
                reverse=True,
            )
            best, fixture = scored[0] if scored else (0.0, None)
            if fixture is None or best < 0.70:
                missing_targets += 1
                self.stdout.write(f"TARGET_MISSING | {home} vs {away} | best={best:.2f}")
                continue
            if fixture.id in seen_fixture_ids:
                continue
            seen_fixture_ids.add(fixture.id)
            matched.append(fixture)

        self.stdout.write(
            f"TARGET BACKFILL PREP | date={DAY} targets={len(TARGETS)} matched={len(matched)} "
            f"target_missing={missing_targets}"
        )

        for fixture in matched:
            if classify_competition(fixture).excluded:
                excluded += 1
                self.stdout.write(
                    f"EXCLUDED | {fixture.home_team.name} vs {fixture.away_team.name}"
                )
                continue

            exists = Prediction.objects.filter(
                fixture=fixture,
                model_version=V8_MODEL_VERSION,
                market__iexact="BTTS",
            ).exists()
            if exists:
                already_predicted += 1
                continue

            cleared, _ = FixtureScoreState.objects.filter(
                fixture=fixture,
                model_version=V8_MODEL_VERSION,
            ).delete()
            stale_cleared += int(cleared)

            self.stdout.write(
                f"SCORING | {fixture.home_team.name} vs {fixture.away_team.name} | external_id={fixture.external_id}"
            )
            call_command("score_v8", fixture_id=str(fixture.external_id))

            now_exists = Prediction.objects.filter(
                fixture=fixture,
                model_version=V8_MODEL_VERSION,
                market__iexact="BTTS",
            ).exists()
            if now_exists:
                created += 1
            else:
                still_missing.append(
                    f"{fixture.home_team.name} vs {fixture.away_team.name}"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"TARGET BACKFILL RESULT | matched={len(matched)} excluded={excluded} "
                f"already_predicted={already_predicted} stale_states_cleared={stale_cleared} "
                f"created={created} still_missing={len(still_missing)}"
            )
        )
        for match in still_missing:
            self.stdout.write(self.style.WARNING(f"STILL_MISSING | {match}"))

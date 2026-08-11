from __future__ import annotations

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import DailyPremiumSelection, Fixture
from .premium_replacement import PremiumReplacementService, fixture_is_operational
from .score_v8 import V8_MODEL_VERSION


@receiver(post_save, sender=Fixture)
def reconcile_premium_after_fixture_status_change(sender, instance: Fixture, created: bool, **kwargs):
    """Immediately refill Premium slots when an official pick becomes unavailable.

    The guard is intentionally narrow: we only reconcile when the changed fixture
    is currently occupying a Premium slot and is no longer operational. This
    avoids expensive re-ranking on ordinary fixture/statistics saves.
    """
    if created or fixture_is_operational(instance):
        return

    selections = DailyPremiumSelection.objects.filter(
        prediction__fixture_id=instance.id,
        model_version=V8_MODEL_VERSION,
    ).values_list("target_date", flat=True).distinct()

    for target_date in selections:
        PremiumReplacementService(model_version=V8_MODEL_VERSION).reconcile(
            target_date,
            trigger=f"fixture_status:{instance.status}",
        )

from django.apps import AppConfig


class EngineConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "engine"

    def ready(self):
        from . import signals  # noqa: F401
        from .btts_h2h_guard import install_h2h_guard
        from .btts_v2_policy import install_btts_v2_policy
        from .btts_v21_policy import install_btts_v21_policy

        install_h2h_guard()
        install_btts_v2_policy()
        install_btts_v21_policy()

from django.apps import AppConfig


class EngineConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "engine"

    def ready(self):
        from . import signals  # noqa: F401
        from .btts_h2h_guard import install_h2h_guard

        install_h2h_guard()

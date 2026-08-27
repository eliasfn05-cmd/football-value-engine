from django.apps import AppConfig


class EngineConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "engine"

    def ready(self):
        from . import signals  # noqa: F401
        from .btts_h2h_guard import install_h2h_guard
        from .btts_v2_policy import install_btts_v2_policy
        from .btts_v21_policy import install_btts_v21_policy
        from .btts_v22_policy import install_btts_v22_policy
        from .btts_v23_policy import install_btts_v23_policy
        from .btts_v24_policy import install_btts_v24_policy
        from .btts_v25_policy import install_btts_v25_policy
        from .btts_v26_policy import install_btts_v26_policy
        from .btts_v27_policy import install_btts_v27_policy
        from .btts_v291_policy import install_btts_v291_policy
        from .btts_v292_policy import install_btts_v292_policy

        install_h2h_guard()
        install_btts_v2_policy()
        install_btts_v21_policy()
        install_btts_v22_policy()
        install_btts_v23_policy()
        install_btts_v24_policy()
        install_btts_v25_policy()
        install_btts_v26_policy()
        install_btts_v27_policy()
        install_btts_v291_policy()
        install_btts_v292_policy()

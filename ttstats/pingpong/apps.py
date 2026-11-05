from django.apps import AppConfig


class PingpongConfig(AppConfig):
    name = 'pingpong'

    def ready(self) -> None:
        import pingpong.signals  # noqa
        import pingpong.flags  # noqa: F401  (registers the system check)
        return super().ready()
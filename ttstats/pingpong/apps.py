from django.apps import AppConfig


class PingpongConfig(AppConfig):
    name = 'pingpong'

    def ready(self) -> None:
        import pingpong.signals  # noqa
        return super().ready()
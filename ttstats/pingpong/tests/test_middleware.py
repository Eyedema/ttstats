import pytest
from django.test import RequestFactory

from ttstats.middleware import CurrentUserMiddleware, _thread_locals, get_current_user
from .conftest import UserFactory


class TestGetCurrentUser:
    def test_returns_none_when_not_set(self):
        if hasattr(_thread_locals, "user"):
            del _thread_locals.user
        assert get_current_user() is None

    def test_returns_user_when_set(self, db):
        u = UserFactory()
        _thread_locals.user = u
        assert get_current_user() == u
        del _thread_locals.user


@pytest.mark.django_db
class TestCurrentUserMiddleware:
    def test_stores_user_during_request(self):
        captured = {}

        def inner(request):
            captured["user"] = get_current_user()
            return "response"

        middleware = CurrentUserMiddleware(inner)
        rf = RequestFactory()
        request = rf.get("/")
        user = UserFactory()
        request.user = user

        middleware(request)
        assert captured["user"] == user

    def test_cleans_up_after_request(self):
        def inner(request):
            return "response"

        middleware = CurrentUserMiddleware(inner)
        rf = RequestFactory()
        request = rf.get("/")
        user = UserFactory()
        request.user = user

        middleware(request)
        assert get_current_user() is None

    def test_cleans_up_on_exception(self):
        def inner(request):
            raise ValueError("boom")

        middleware = CurrentUserMiddleware(inner)
        rf = RequestFactory()
        request = rf.get("/")
        user = UserFactory()
        request.user = user

        with pytest.raises(ValueError):
            middleware(request)
        assert get_current_user() is None


@pytest.mark.django_db
class TestClearOnEntry:
    """KAN-168 runs gunicorn gthread: a worker thread serves request after
    request. Whatever the previous one left on the thread-local (a crash
    above the middleware, a hand-set test value) must not reach the next."""

    def test_a_leftover_club_and_memberships_are_dropped_on_the_way_in(self):
        stale = object()
        _thread_locals.club = stale
        _thread_locals.user_clubs = [stale]
        _thread_locals.user_memberships = [stale]
        _thread_locals.flag_memo = {"clubs": True}
        seen = {}

        def inner(request):
            for attr in ("club", "user_clubs", "user_memberships"):
                seen[attr] = getattr(_thread_locals, attr, None)
            seen["flag_memo"] = dict(_thread_locals.flag_memo)
            return "response"

        request = RequestFactory().get("/")
        request.user = UserFactory()
        CurrentUserMiddleware(inner)(request)

        assert seen == {
            "club": None,
            "user_clubs": None,
            "user_memberships": None,
            "flag_memo": {},
        }

    def test_thread_locals_are_per_thread(self):
        """Two gthread threads never see each other's request state."""
        import threading

        _thread_locals.club = "main"
        seen = {}

        def other():
            seen["club"] = getattr(_thread_locals, "club", None)

        t = threading.Thread(target=other)
        t.start()
        t.join()
        del _thread_locals.club
        assert seen["club"] is None

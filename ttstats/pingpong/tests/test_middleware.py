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

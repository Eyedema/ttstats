"""Feature-flag helper (KAN-11 / T1.9).

The contract these tests pin down is *fail closed*: a flag nobody has
deliberately turned on is off, and an unknown flag is off rather than an
exception. Every risky rollout in the backlog leans on that.
"""
import pytest
from django.contrib.auth.models import AnonymousUser
from django.template import Context, Template
from django.test import override_settings

from pingpong import flags
from pingpong.models import FeatureFlagOverride

from .conftest import UserFactory


class TestIsEnabledWithoutDatabase:
    """The settings-only paths, which must work with no models loaded.

    Deliberately not marked django_db: a management command or a migration
    checking a flag has no request, no user, and sometimes no usable DB.
    """

    @override_settings(FEATURE_FLAGS={"clubs": False})
    def test_flag_declared_false_is_off_with_no_user(self):
        assert flags.is_enabled("clubs") is False

    @override_settings(FEATURE_FLAGS={"clubs": True})
    def test_flag_declared_true_is_on_with_no_user(self):
        assert flags.is_enabled("clubs") is True

    @override_settings(FEATURE_FLAGS={"clubs": False})
    def test_unknown_flag_is_off_and_does_not_raise(self):
        assert flags.is_enabled("no_such_flag") is False

    @override_settings(FEATURE_FLAGS={"clubs": False})
    def test_unknown_flag_logs_a_warning(self, caplog):
        flags.is_enabled("no_such_flag")
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("no_such_flag" in r.getMessage() for r in warnings)

    @override_settings(FEATURE_FLAGS={"clubs": True})
    def test_anonymous_user_gets_the_settings_default(self):
        assert flags.is_enabled("clubs", AnonymousUser()) is True

    @override_settings(FEATURE_FLAGS={"clubs": False})
    def test_anonymous_user_never_gets_an_override(self):
        assert flags.is_enabled("clubs", AnonymousUser()) is False

    @override_settings(FEATURE_FLAGS={"clubs": False})
    def test_cache_failure_falls_back_to_settings(self, monkeypatch, mocker=None):
        """A dead Redis must not turn a flag check into a 500."""
        import pingpong.flags as flags_module

        class BrokenCache:
            def get(self, *a, **k):
                raise RuntimeError("redis is down")

            def set(self, *a, **k):
                raise RuntimeError("redis is down")

            def delete(self, *a, **k):
                raise RuntimeError("redis is down")

        monkeypatch.setattr(flags_module, "cache", BrokenCache())
        assert flags.is_enabled("clubs") is False

    @override_settings(FEATURE_FLAGS={"clubs": True})
    def test_all_flags_reports_declared_flags(self):
        assert flags.all_flags() == {"clubs": True}


@pytest.mark.django_db
class TestPerUserOverrides:
    @override_settings(FEATURE_FLAGS={"clubs": False})
    def test_override_enables_for_one_user_only(self):
        user_a = UserFactory()
        user_b = UserFactory()
        FeatureFlagOverride.objects.create(name="clubs", user=user_a, enabled=True)

        assert flags.is_enabled("clubs", user_a) is True
        assert flags.is_enabled("clubs", user_b) is False

    @override_settings(FEATURE_FLAGS={"clubs": True})
    def test_override_can_also_disable_for_one_user(self):
        """Overrides win in both directions — that is the kill switch."""
        user_a = UserFactory()
        user_b = UserFactory()
        FeatureFlagOverride.objects.create(name="clubs", user=user_a, enabled=False)

        assert flags.is_enabled("clubs", user_a) is False
        assert flags.is_enabled("clubs", user_b) is True

    @override_settings(FEATURE_FLAGS={"clubs": False})
    def test_override_for_an_unknown_flag_is_still_off(self):
        """An override row must not be able to invent a flag."""
        user = UserFactory()
        FeatureFlagOverride.objects.create(name="ghost", user=user, enabled=True)

        assert flags.is_enabled("ghost", user) is False

    @override_settings(FEATURE_FLAGS={"clubs": False})
    def test_override_is_cached_after_the_first_lookup(self, django_assert_num_queries):
        user = UserFactory()
        FeatureFlagOverride.objects.create(name="clubs", user=user, enabled=True)

        with django_assert_num_queries(1):
            assert flags.is_enabled("clubs", user) is True
        with django_assert_num_queries(0):
            assert flags.is_enabled("clubs", user) is True

    @override_settings(FEATURE_FLAGS={"clubs": False})
    def test_saving_an_override_invalidates_the_cached_value(self):
        user = UserFactory()
        assert flags.is_enabled("clubs", user) is False

        FeatureFlagOverride.objects.create(name="clubs", user=user, enabled=True)
        assert flags.is_enabled("clubs", user) is True

    @override_settings(FEATURE_FLAGS={"clubs": False})
    def test_deleting_an_override_invalidates_the_cached_value(self):
        user = UserFactory()
        override = FeatureFlagOverride.objects.create(
            name="clubs", user=user, enabled=True
        )
        assert flags.is_enabled("clubs", user) is True

        override.delete()
        assert flags.is_enabled("clubs", user) is False

    def test_same_user_and_flag_cannot_be_overridden_twice(self):
        from django.db import IntegrityError, transaction

        user = UserFactory()
        FeatureFlagOverride.objects.create(name="clubs", user=user, enabled=True)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FeatureFlagOverride.objects.create(
                    name="clubs", user=user, enabled=False
                )


class TestRequestScopedMemo:
    """`managers.get_queryset()` runs constantly; it must not hit the cache
    backend once per query."""

    @override_settings(FEATURE_FLAGS={"clubs": False})
    def test_memo_is_used_within_a_request_scope(self):
        calls = []
        with flags.request_scope():
            flags.is_enabled("clubs")
            flags.is_enabled("clubs")
            calls.append(flags._request_memo())
        assert calls[0] is not None
        assert ("clubs", None) in calls[0]

    @override_settings(FEATURE_FLAGS={"clubs": False})
    def test_memo_is_cleared_when_the_scope_exits(self):
        with flags.request_scope():
            flags.is_enabled("clubs")
        assert flags._request_memo() is None

    @override_settings(FEATURE_FLAGS={"clubs": False})
    def test_memo_is_cleared_even_when_the_scope_raises(self):
        with pytest.raises(ValueError):
            with flags.request_scope():
                flags.is_enabled("clubs")
                raise ValueError("boom")
        assert flags._request_memo() is None


class TestTemplateTag:
    def _render(self, template, context=None):
        return Template(template).render(Context(context or {}))

    @override_settings(FEATURE_FLAGS={"clubs": False})
    def test_guarded_block_is_absent_when_the_flag_is_off(self):
        out = self._render(
            '{% load feature_tags %}'
            '{% feature_enabled "clubs" as clubs_on %}'
            '{% if clubs_on %}SECRET-MARKER{% endif %}'
        )
        assert "SECRET-MARKER" not in out

    @override_settings(FEATURE_FLAGS={"clubs": True})
    def test_guarded_block_renders_when_the_flag_is_on(self):
        out = self._render(
            '{% load feature_tags %}'
            '{% feature_enabled "clubs" as clubs_on %}'
            '{% if clubs_on %}SECRET-MARKER{% endif %}'
        )
        assert "SECRET-MARKER" in out

    @override_settings(FEATURE_FLAGS={"clubs": False})
    def test_unknown_flag_in_a_template_renders_nothing_and_does_not_raise(self):
        out = self._render(
            '{% load feature_tags %}'
            '{% feature_enabled "ghost" as on %}'
            '{% if on %}SECRET-MARKER{% endif %}'
        )
        assert "SECRET-MARKER" not in out

    @override_settings(FEATURE_FLAGS={"clubs": False})
    @pytest.mark.django_db
    def test_tag_picks_the_user_up_from_the_request_in_context(self):
        from django.test import RequestFactory

        user = UserFactory()
        FeatureFlagOverride.objects.create(name="clubs", user=user, enabled=True)
        request = RequestFactory().get("/")
        request.user = user

        out = self._render(
            '{% load feature_tags %}'
            '{% feature_enabled "clubs" as clubs_on %}'
            '{% if clubs_on %}SECRET-MARKER{% endif %}',
            {"request": request},
        )
        assert "SECRET-MARKER" in out

"""Template access to feature flags.

Deliberately an assignment tag rather than a block tag, so the guard is a plain
``{% if %}`` and the guarded markup is *absent* from the response rather than
hidden with CSS -- a flag that only hides a control still ships the control.

    {% load feature_tags %}
    {% feature_enabled "clubs" as clubs_on %}
    {% if clubs_on %} ... {% endif %}

The user comes from ``request`` in the context, so a template does not need a
context-processor entry per flag.
"""
from django import template

from .. import flags

register = template.Library()


@register.simple_tag(takes_context=True)
def feature_enabled(context, name):
    request = context.get("request")
    user = getattr(request, "user", None) if request else None
    return flags.is_enabled(name, user)

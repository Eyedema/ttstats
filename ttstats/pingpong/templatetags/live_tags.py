"""Template tags for the live scoreboard.

``{% live_rules_config %}`` emits ``live_scoring.rules_config()`` as a
``json_script`` element, which ``static/pingpong/js/live_rules.js`` is built
from (``LiveRules.create(JSON.parse(el.textContent))``). The constants are
never re-typed in JavaScript.
"""
from django import template
from django.utils.html import json_script

from .. import live_scoring

register = template.Library()

LIVE_RULES_CONFIG_ID = "live-rules-config"


@register.simple_tag
def live_rules_config(element_id=LIVE_RULES_CONFIG_ID):
    return json_script(live_scoring.rules_config(), element_id)

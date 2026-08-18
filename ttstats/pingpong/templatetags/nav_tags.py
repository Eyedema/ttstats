"""Navigation state for the tab bar, the drawer and the desktop sidebar.

The active test is a tag rather than an expression at the call site because
Django's `{% include with %}` cannot evaluate a comparison: writing
`active=nav_tab == 'today'` passes the *string* `nav_tab`, which is truthy, and
every row in the menu lights up at once. That is a silent failure with no error
anywhere, so the comparison lives in Python where it can be tested.
"""

from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def nav_active(context, match_tab="", match_urls=""):
    """True when the current page belongs to this navigation row.

    `match_tab` matches against the four tab destinations resolved by
    context_processors.TAB_FOR_URL_NAME (so a sub-page like the live scoreboard
    still lights up Play). `match_urls` is a space-separated list of url_names
    for the drawer's own destinations, which are not tabs and therefore have no
    tab to inherit from.
    """
    if match_tab and context.get("nav_tab") == match_tab:
        return True

    request = context.get("request")
    resolver = getattr(request, "resolver_match", None)
    url_name = getattr(resolver, "url_name", None)
    return bool(url_name and url_name in match_urls.split())

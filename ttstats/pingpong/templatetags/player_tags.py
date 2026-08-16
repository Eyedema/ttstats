from django import template
from django.urls import reverse
from django.utils.html import format_html

register = template.Library()


@register.simple_tag
def player_link(player, css="", label=None):
    """Link to a player's profile, or plain text if that player is gone.

    MatchParticipant.player cascades on delete, so removing a player strips
    their participant rows and leaves their old matches with an empty side.
    Templates then called `{% url 'player_detail' <empty> %}`, which raises
    NoReverseMatch -- a 500 on the match list for everyone, permanently,
    triggered by an ordinary admin deletion.

    `label` overrides the link text; it defaults to the player's own str,
    and falls back to a placeholder when there is no player at all.
    """
    text = label if label is not None else (str(player) if player else "Unknown player")
    if player is not None and getattr(player, "pk", None):
        return format_html(
            '<a href="{}" class="{}">{}</a>',
            reverse("pingpong:player_detail", args=[player.pk]),
            css,
            text,
        )
    return format_html('<span class="{}">{}</span>', css, text)

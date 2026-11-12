"""The `{% qr_svg %}` tag: a QR code as inline SVG (KAN-163, for the KAN-151 lobby).

Built from segno's module matrix rather than segno's SVG writer so the markup
is ours to guarantee:

- **No `style=` attribute anywhere.** Prod's CSP would block it. Colour comes
  from the `.qr-ink` / `.qr-paper` classes in app.css, which read the
  `--qr-ink` / `--qr-paper` tokens. Those tokens do not flip with the theme:
  scanners need dark modules on a light field.
- The `fill` presentation attributes are the stylesheet-failed fallback
  (black on white, still scannable); the classes override them.
- **One `<path>`** for every dark module, one horizontal run per `h` segment,
  so a 33x33 code is a few KB rather than a `<rect>` per module.
- **`viewBox` and no width/height**, so the SVG fills its container; the page
  decides the size (the lobby uses ~70% of the viewport width).
- Error correction M, quiet zone of 4 modules (the spec minimum).
"""

import segno
from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

ERROR_LEVEL = "m"
QUIET_ZONE = 4  # modules; ISO/IEC 18004 minimum


def qr_matrix(data):
    """The module matrix for `data`, without the quiet zone: rows of 0/1."""
    return segno.make(data, error=ERROR_LEVEL).matrix


def path_data(matrix, border=QUIET_ZONE):
    """SVG path data drawing each horizontal run of dark modules as a 1-high box.

    Coordinates are in modules, offset by `border` so the quiet zone is empty
    space inside the viewBox.
    """
    parts = []
    for y, row in enumerate(matrix):
        x = 0
        width = len(row)
        while x < width:
            if not row[x]:
                x += 1
                continue
            start = x
            while x < width and row[x]:
                x += 1
            run = x - start
            parts.append(f"M{start + border} {y + border}h{run}v1h-{run}z")
    return "".join(parts)


@register.simple_tag
def qr_svg(data, label="", css=""):
    """Render `data` (normally an absolute URL) as an inline SVG QR code.

    `label` is the accessible name; it defaults to the encoded text. `css`
    adds classes to the <svg> (for sizing at the call site).
    """
    matrix = qr_matrix(str(data))
    size = len(matrix) + 2 * QUIET_ZONE
    classes = f"qr {css}".strip()
    # format_html escapes `label` and `classes`; the numbers and path data are
    # built here from integers, so they cannot carry markup.
    svg = format_html(
        '<svg xmlns="http://www.w3.org/2000/svg" class="{}" viewBox="0 0 {} {}" '
        'role="img" aria-label="{}" shape-rendering="crispEdges">'
        '<rect class="qr-paper" fill="#fff" width="{}" height="{}"/>'
        '<path class="qr-ink" fill="#000" d="{}"/>'
        "</svg>",
        classes,
        size,
        size,
        label or str(data),
        size,
        size,
        path_data(matrix),
    )
    return mark_safe(svg)

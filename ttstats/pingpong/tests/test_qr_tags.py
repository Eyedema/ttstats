"""The `{% qr_svg %}` tag (KAN-163). No database needed."""

import pathlib
import re
import xml.etree.ElementTree as ET

import segno
from django.template import Context, Template

from pingpong.templatetags.qr_tags import QUIET_ZONE, path_data, qr_svg

SVG_NS = "{http://www.w3.org/2000/svg}"
URL = "https://pingpong.example.org/pingpong/live/join/0123456789abcdef0123456789abcdef/"
APP_CSS = pathlib.Path(__file__).resolve().parents[1] / "assets" / "app.css"

RUN = re.compile(r"M(\d+) (\d+)h(\d+)v1h-(\d+)z")


def _svg(url=URL, **kwargs):
    return ET.fromstring(str(qr_svg(url, **kwargs)))


def _matrix_from_path(d, size):
    """Paint the path's runs back onto a size x size grid (quiet zone included)."""
    assert RUN.sub("", d) == "", "path data must be only horizontal runs"
    grid = [[0] * size for _ in range(size)]
    for x, y, run, back in RUN.findall(d):
        x, y, run = int(x), int(y), int(run)
        assert int(back) == run
        for i in range(run):
            assert grid[y][x + i] == 0, "runs must not overlap"
            grid[y][x + i] = 1
    return grid


def _decoded(url=URL):
    svg = _svg(url)
    size = int(svg.get("viewBox").split()[2])
    path = svg.find(f"{SVG_NS}path")
    return _matrix_from_path(path.get("d"), size), size


class TestQrSvgMarkup:
    def test_no_style_attribute_anywhere(self):
        html = str(qr_svg(URL, label='Join "our" match'))
        assert "style" not in html.lower()

    def test_role_img_and_aria_label(self):
        svg = _svg(label="Scan to join the match")
        assert svg.get("role") == "img"
        assert svg.get("aria-label") == "Scan to join the match"

    def test_label_defaults_to_the_url(self):
        assert _svg().get("aria-label") == URL

    def test_label_and_url_are_escaped(self):
        url = 'https://x.org/?a=1&b="2"<script>'
        html = str(qr_svg(url, label='<b>"hi"</b> & bye'))
        assert "<script>" not in html and "<b>" not in html
        svg = ET.fromstring(html)  # well-formed despite the hostile input
        assert svg.get("aria-label") == '<b>"hi"</b> & bye'
        assert _svg(url).get("aria-label") == url

    def test_exactly_one_path(self):
        svg = _svg()
        assert len(svg.findall(f"{SVG_NS}path")) == 1
        assert [c.tag for c in svg] == [f"{SVG_NS}rect", f"{SVG_NS}path"]

    def test_scales_to_container_via_viewbox(self):
        svg = _svg(css="w-full")
        assert svg.get("width") is None and svg.get("height") is None
        assert svg.get("viewBox").startswith("0 0 ")
        assert svg.get("class").split() == ["qr", "w-full"]

    def test_colour_classes_with_unstyled_fallback(self):
        svg = _svg()
        rect, path = list(svg)
        assert rect.get("class") == "qr-paper" and rect.get("fill") == "#fff"
        assert path.get("class") == "qr-ink" and path.get("fill") == "#000"

    def test_renders_through_the_template_tag(self):
        out = Template('{% load qr_tags %}{% qr_svg url label="Join" %}').render(
            Context({"url": URL})
        )
        assert out.startswith("<svg") and 'aria-label="Join"' in out
        assert "&lt;" not in out  # mark_safe: not double-escaped


class TestQrSvgMatrix:
    def test_drawn_modules_round_trip_to_segno_matrix(self):
        grid, size = _decoded()
        expected = segno.make(URL, error="m").matrix
        n = len(expected)
        assert size == n + 2 * QUIET_ZONE
        inner = [row[QUIET_ZONE : QUIET_ZONE + n] for row in grid[QUIET_ZONE : QUIET_ZONE + n]]
        assert inner == [list(r) for r in expected]

    def test_error_correction_is_m(self):
        assert segno.make(URL, error="m").error == "M"
        # A different level would give a different matrix; ours matches M's.
        grid, _ = _decoded()
        assert any(v for row in grid for v in row)

    def test_quiet_zone_is_at_least_four_modules_and_empty(self):
        grid, size = _decoded()
        assert QUIET_ZONE >= 4
        for y in range(size):
            for x in range(size):
                if min(x, y, size - 1 - x, size - 1 - y) < QUIET_ZONE:
                    assert grid[y][x] == 0

    def test_path_data_merges_runs(self):
        assert path_data([[1, 1, 0, 1]], border=0) == "M0 0h2v1h-2zM3 0h1v1h-1z"
        assert path_data([[0, 0]], border=4) == ""


class TestQrTokens:
    def _blocks(self):
        css = APP_CSS.read_text()
        root_end = css.index("}", css.index(":root {"))
        return css, css[css.index(":root {") : root_end]

    def test_tokens_declared_dark_on_light(self):
        _, root = self._blocks()
        assert "--qr-ink: 0 0 0;" in root
        assert "--qr-paper: 255 255 255;" in root

    def test_tokens_do_not_flip_with_the_theme(self):
        css, _ = self._blocks()
        # Declared once only: no theme override (light media query, data-theme,
        # or anything else) may redefine them.
        assert css.count("--qr-ink:") == 1
        assert css.count("--qr-paper:") == 1

    def test_classes_are_outside_every_layer(self):
        css = APP_CSS.read_text()
        qr_rule = css.index(".qr-ink {")
        # Top level (brace depth 0): Tailwind would purge a layered rule whose
        # class name appears only in Python.
        before = re.sub(r"/\*.*?\*/", "", css[:qr_rule], flags=re.DOTALL)
        assert before.count("{") == before.count("}")

#!/usr/bin/env python3
"""Report Tailwind classes used in templates that the built CSS does not define.

Purging is silent: a class the scanner never saw simply produces no rule, the
page renders unstyled, and nothing in the test suite notices. This script is
the automated stand-in for eyeballing screenshots.

Usage:
    npm run build:css && python scripts/check_css_coverage.py

Exit code is 1 if anything is missing. Expect some false positives -- dynamic
values, Alpine expressions and non-Tailwind hook classes all look like classes
to a regex -- so read the output rather than wiring this into CI as a gate.
"""
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CSS = ROOT / "ttstats/pingpong/static/pingpong/css/app.css"
TEMPLATES = ROOT / "ttstats/pingpong/templates"
FORMS = ROOT / "ttstats/pingpong/forms.py"

# class="..." and class='...' in templates, plus the Python widget class strings.
# The negative lookbehind skips Alpine's `:class` / `x-bind:class`, whose value
# is a JavaScript expression rather than a list of classes -- scanning it as one
# reports `currentServer` and `state.team1_games` as missing utilities and
# buries the real findings. The literal class names inside those expressions are
# picked up separately by ALPINE_CLASS below, which is what Tailwind's own
# scanner sees too.
CLASS_ATTR = re.compile(r"""(?<![:\w-])class\s*=\s*["']([^"']*)["']""")
# Quoted string literals inside an Alpine class binding: the only parts of the
# expression that are actually class names.
ALPINE_CLASS = re.compile(r'(?::|x-bind:)class\s*=\s*"([^"]*)"')
ALPINE_LITERAL = re.compile(r"'([^']*)'")
# Django tags inside a class attribute. Stripping the delimiters and their
# contents leaves the literal classes from every branch, which is what we want:
# `{% if x %}bg-success{% else %}bg-muted{% endif %}` -> `bg-success bg-muted`.
# Without this the tag internals (`if`, `endif`, `form.name.errors`) all look
# like classes and bury the real findings.
TEMPLATE_TAG = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
# A token that looks like a utility rather than prose.
TOKEN = re.compile(r"^[a-z][a-z0-9:\[\]\-_./\\%!()]*$", re.IGNORECASE)

# Classes the app defines itself or that come from a library, not Tailwind.
IGNORE_PREFIXES = (
    "js-",
    "alpine",
    "htmx-",
)
IGNORE_EXACT = {
    # Defined by the app's own CSS, or used only as a JS/CSS hook.
    "mobile-stack-table",
    "singles-only",
    "doubles-only",
    "doubles-required",
    "player-select",
    "no-results",  # Tom Select dropdown internals
    "button",  # Django admin's own class, in the admin/ template overrides
    "hidden",  # real Tailwind class, but also toggled by JS; keep noise down
}


def used_classes() -> set[str]:
    found: set[str] = set()
    sources = list(TEMPLATES.rglob("*.html"))
    if FORMS.exists():
        sources.append(FORMS)
    for path in sources:
        text = path.read_text(encoding="utf-8", errors="replace")
        # Strip Django tags from the whole file *before* looking for class
        # attributes. A single quote inside a tag -- {% if f == 'all' %} --
        # otherwise terminates the attribute regex early and leaks the tag's
        # internals into the results.
        text = TEMPLATE_TAG.sub(" ", text)
        for match in CLASS_ATTR.finditer(text):
            for token in match.group(1).split():
                found.add(token)
        for match in ALPINE_CLASS.finditer(text):
            for literal in ALPINE_LITERAL.findall(match.group(1)):
                found.update(literal.split())
        # forms.py builds bare class strings rather than class="..." attrs.
        # Parse it instead of scanning lines -- a line-based reader picks up
        # dict keys like `'class': INPUT_CSS` as though they were classes.
        if path == FORMS:
            found.update(_class_strings_in_python(text))
    return found


def _class_strings_in_python(source: str) -> set[str]:
    """Every string literal in a .py file that reads like a list of classes.

    Heuristic: several whitespace-separated tokens, most of which carry a
    hyphen or a variant colon. Real utility strings are dense with those
    (`h-12 w-full rounded-md md:text-sm`); help text and error messages are
    not, so prose does not leak in.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        tokens = node.value.split()
        if len(tokens) < 2 or not all(TOKEN.match(t) for t in tokens):
            continue
        utility_ish = sum(1 for t in tokens if "-" in t or ":" in t)
        if utility_ish >= 0.6 * len(tokens):
            found.update(tokens)
    return found


def defined_classes(css: str) -> set[str]:
    # Selectors in the built file are CSS-escaped: `md\:text-sm`, `w-1\/2`.
    return {
        re.sub(r"\\(.)", r"\1", name)
        for name in re.findall(r"\.((?:[^\s{},:>+~\[\]\\]|\\.)+)", css)
    }


def main() -> int:
    if not CSS.exists():
        print(f"error: {CSS.relative_to(ROOT)} not found -- run `npm run build:css`")
        return 1

    defined = defined_classes(CSS.read_text())
    missing = []
    for cls in sorted(used_classes()):
        if not TOKEN.match(cls):
            continue  # not a literal class name
        if cls in IGNORE_EXACT or cls.startswith(IGNORE_PREFIXES):
            continue
        if cls not in defined:
            missing.append(cls)

    if missing:
        print(f"{len(missing)} class(es) used but not present in the built CSS:\n")
        for cls in missing:
            print(f"  {cls}")
        print("\nEach is either a purge miss (fix the `content` globs in")
        print("tailwind.config.js) or a non-Tailwind hook class (add it to")
        print("IGNORE_EXACT in this script).")
        return 1

    print(f"OK -- every class used in templates is present in {CSS.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

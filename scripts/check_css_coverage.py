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
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CSS = ROOT / "ttstats/pingpong/static/pingpong/css/app.css"
TEMPLATES = ROOT / "ttstats/pingpong/templates"
FORMS = ROOT / "ttstats/pingpong/forms.py"

# class="..." and class='...' in templates, plus the Python widget class strings.
CLASS_ATTR = re.compile(r"""class\s*=\s*["']([^"']*)["']""")
# A token that looks like a utility rather than prose or a template expression.
TOKEN = re.compile(r"^[a-z0-9:\[\]\-_./\\%!()]+$", re.IGNORECASE)

# Classes the app defines itself or that come from a library, not Tailwind.
IGNORE_PREFIXES = (
    "js-",
    "alpine",
    "htmx-",
)
IGNORE_EXACT = {
    "mobile-stack-table",
    "singles-only",
    "doubles-only",
    "hidden",  # real Tailwind class, but also toggled by JS; keep noise down
}


def used_classes() -> set[str]:
    found: set[str] = set()
    sources = list(TEMPLATES.rglob("*.html"))
    if FORMS.exists():
        sources.append(FORMS)
    for path in sources:
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in CLASS_ATTR.finditer(text):
            for token in match.group(1).split():
                found.add(token)
        # forms.py builds bare class strings, not class="..." attributes.
        if path == FORMS:
            for line in text.splitlines():
                if "_CSS" in line and "=" in line and "'" in line:
                    inner = line.split("'")
                    if len(inner) > 1:
                        found.update(inner[1].split())
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
        if not TOKEN.match(cls) or "{" in cls or "}" in cls:
            continue  # template expression, not a literal class
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

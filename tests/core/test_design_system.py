"""Gates for the design system.

These run in the normal suite, which is the point: the design work that landed
in `feat/design-system` is a set of decisions written into one stylesheet, and
nothing stopped the next hardcoded hex from quietly undoing it. Each test below
fails on a specific way that has already happened once in this repo.

The browser-based audits -- axe-core, state-aware contrast, reflow at 280px --
are not here. They need Playwright and a booted app, and the scripts live in a
sibling repository rather than in this one. They stay a manual step; see
`docs/DESIGN.md`. What is here is everything checkable from the source alone,
and it is the part that catches drift rather than regressions in rendering.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
THEME = ROOT / "app" / "core" / "static" / "css" / "theme.css"
APP = ROOT / "app"

# Pictographs. Arrows and box-drawing are deliberately allowed -- they carry
# meaning in the task-schedule tables and read as typography, not decoration.
EMOJI = re.compile(
    "["
    "\U0001f300-\U0001faff"
    "\U00002600-\U000026ff"
    "\U00002700-\U000027bf"
    "\U0001f000-\U0001f2ff"
    "\U0000fe0f"
    "\U00002b00-\U00002bff"
    "]"
)


def _css() -> str:
    return THEME.read_text(encoding="utf-8")


def _product_files() -> list[Path]:
    return [
        p
        for pattern in ("*.html", "*.py", "*.js")
        for p in APP.rglob(pattern)
        if "__pycache__" not in p.parts and p.name != "htmx.min.js"
    ]


# --------------------------------------------------------------------------
# Colour maths, kept here rather than imported so the gate has no dependency
# that could drift from what the browser actually computes.
# --------------------------------------------------------------------------


def _linear(channel: int) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)


def contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _tokens() -> dict[str, str]:
    return {
        name: value
        for name, value in re.findall(r"(--[a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})\s*;", _css())
    }


# --------------------------------------------------------------------------


def test_type_scale_has_no_literal_font_sizes():
    """Every size comes from the scale.

    There were twenty literals in this file before the scale existed, several
    below the 11px floor. A new literal is how that comes back.
    """
    literals = re.findall(r"font-size:\s*([0-9.]+(?:px|rem|em))", _css())
    assert literals == [], f"theme.css must size type from the scale, found: {literals}"


def test_templates_size_type_from_the_scale():
    offenders: dict[str, list[str]] = {}
    for path in APP.rglob("*.html"):
        found = [
            value
            for value in re.findall(
                r"font-size:\s*((?:[0-9]*\.)?[0-9]+(?:px|rem|em))",
                path.read_text(encoding="utf-8"),
            )
            # 0.5em on the notification badge is relative to its parent by
            # design -- it scales with the badge rather than with the page.
            if value != "0.5em"
        ]
        if found:
            offenders[str(path.relative_to(ROOT))] = found
    assert offenders == {}, f"inline font-size must use a --text-* token: {offenders}"


def test_product_ui_carries_no_emoji():
    """Emoji read as machine-generated, and one was reaching the database.

    The Bluesky adapter prefixed stored note text with a link glyph, so the
    emoji outlived the template it came from.
    """
    offenders: dict[str, list[str]] = {}
    for path in _product_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found = EMOJI.findall(text)
        if found:
            offenders[str(path.relative_to(ROOT))] = found
    assert offenders == {}, f"use a Bootstrap icon or words instead: {offenders}"


@pytest.mark.parametrize("family", ["cat", "q"])
def test_tint_and_ink_pairs_clear_aa(family):
    """Badge text must clear AA on the tint it sits on.

    Categorical and quartile colours are the ones picked by eye historically,
    and the pair is what matters -- either half can be changed alone and break
    the other.
    """
    tokens = _tokens()
    if family == "cat":
        pairs = [
            (n, n.replace("-tint", "-ink"))
            for n in tokens
            if n.startswith("--cat-") and n.endswith("-tint")
        ]
    else:
        pairs = [
            (n, n.replace("-tint", "-ink")) for n in tokens if re.fullmatch(r"--q[1-4]-tint", n)
        ]

    assert pairs, f"no {family} tint/ink pairs found -- did the token names change?"
    failures = []
    for tint_name, ink_name in sorted(pairs):
        ratio = contrast(tokens[ink_name], tokens[tint_name])
        if ratio < 4.5:
            failures.append(f"{ink_name} on {tint_name} = {ratio:.2f}:1")
    assert failures == [], f"below WCAG AA: {failures}"


def test_quartile_ramp_stays_ordinal():
    """Q1..Q4 is a ranking, so its tints must get monotonically lighter.

    They were categorical hues once -- Q1 green, Q2 blue, Q3 and Q4 two greys
    nobody could separate. A ramp that stops being monotonic has silently gone
    back to being a set of categories.
    """
    tokens = _tokens()
    tints = [tokens[f"--q{i}-tint"] for i in range(1, 5)]
    lums = [_luminance(t) for t in tints]
    assert all(
        lums[i] < lums[i + 1] for i in range(3)
    ), f"quartile tints must lighten from Q1 to Q4, got {[round(x, 3) for x in lums]}"


def test_documented_contrast_ratios_are_true():
    """The ratios written beside the tokens must be the measured ones.

    Every token carries its contrast in a trailing comment. A comment that
    drifts from its value is worse than no comment, because it is the thing a
    reader trusts instead of measuring.
    """
    ground = _tokens()["--neutral-50"]
    documented = re.findall(
        r"(--(?:brand|neutral)-\d+|--success|--warning|--danger|--info):"
        r"\s*(#[0-9A-Fa-f]{6});\s*/\*\s*([0-9.]+):1",
        _css(),
    )
    assert documented, "no documented ratios found -- did the comment format change?"
    wrong = []
    for name, value, claimed in documented:
        actual = contrast(value, ground)
        if abs(actual - float(claimed)) > 0.1:
            wrong.append(f"{name}: comment says {claimed}:1, measured {actual:.1f}:1")
    assert wrong == [], f"stale contrast comments: {wrong}"


def test_brand_and_danger_stay_separable_by_form_not_hue():
    """The rule the palette cannot enforce on its own.

    Brand and danger are both red. The stylesheet's answer is that destructive
    actions take an outline and never appear as a filled button beside a filled
    primary. If someone gives .btn-danger a brand-coloured fill, or drops the
    outline variant, that answer is gone -- so both must stay declared.
    """
    css = _css()
    assert ".btn-outline-danger {" in css, "the destructive outline variant is the form rule"
    assert ".btn-outline-primary {" in css, "the neutral secondary keeps red meaning danger"
    tokens = _tokens()
    assert (
        contrast(tokens["--danger"], tokens["--neutral-50"]) >= 4.5
    ), "--danger must clear AA on the page ground"

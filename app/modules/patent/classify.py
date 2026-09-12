"""Is this patent an AI patent?

There is no fact of the matter, only a line someone drew. USPTO's own AI
Patent Dataset draws one with a trained classifier, but it is an annual
research release running more than a year behind — it can never cover a
three-week window, so it is not an option here (`docs/PHASE8.md` §3.3).

What is left is CPC classification, which is at least explicit about where the
line is. Two tiers, so the choice is visible in the data rather than buried in
a config value: `cpc_core` is G06N (machine learning proper), `cpc_extended`
adds the applied fields that are AI in practice but classified by application.

A prefix match is correct here: CPC is hierarchical, so `G06N` subsumes
`G06N3/08`, and matching prefixes is how you say "this subtree".
"""

from __future__ import annotations

CORE_DEFAULT = ("G06N",)
EXTENDED_DEFAULT = ("G06V", "G10L", "G06F40")


def normalise(codes: list[str] | None) -> list[str]:
    return [c.strip().upper().replace(" ", "") for c in (codes or []) if c and c.strip()]


def classify(
    cpc_codes: list[str] | None,
    *,
    core: tuple[str, ...] = CORE_DEFAULT,
    extended: tuple[str, ...] = (),
) -> str | None:
    """Return `"cpc_core"`, `"cpc_extended"`, or None if the patent is out.

    Core wins over extended: a patent classified both G06N and G06V is machine
    learning that happens to be applied to vision, and counting it as
    "extended" would understate the core set.
    """
    codes = normalise(cpc_codes)
    if not codes:
        return None
    if any(code.startswith(prefix) for code in codes for prefix in core):
        return "cpc_core"
    if extended and any(code.startswith(prefix) for code in codes for prefix in extended):
        return "cpc_extended"
    return None

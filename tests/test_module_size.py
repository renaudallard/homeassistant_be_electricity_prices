"""Every module of the integration stays under a thousand lines.

Past that a module holds two jobs, and each of the ones that got there was
split along a seam it already had: the coordinator_* mixins, the
_<supplier>_cards / _overlays readers, the meter_* layers over the recorder
reads. Split along one like those rather than raising the limit.
"""

from pathlib import Path

_PACKAGE = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "be_electricity_prices"
)
_MAX_LINES = 1000


def test_every_integration_module_stays_under_a_thousand_lines() -> None:
    """Lines are counted as newlines, the way an editor and ``wc -l`` count
    them: three provider modules carry a U+2028 that ``str.splitlines`` would
    take for a line break."""
    too_long: dict[str, int] = {}
    for path in sorted(_PACKAGE.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").count("\n")
        if lines > _MAX_LINES:
            too_long[str(path.relative_to(_PACKAGE))] = lines
    assert not too_long, too_long

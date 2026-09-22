# Copyright (c) 2026, Renaud Allard <renaud@allard.it>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""Bolt: the regulated legs its card reprints.

The distribution and transport tariffs for all three regions, the levies and
the taxes. Bolt prints them in a three-column layout whose blank cells mean
different things in different rows, which is why the row readers live here
beside the tables they read.
"""

from __future__ import annotations

from ..const import DSO_RESA
from ..const import DSO_REW
from ..const import DSO_SIBELGA
from ..const import REGION_BRUSSELS
from ..const import REGION_FLANDERS
from ..const import REGION_WALLONIA
from ._parse import parse_brussels_osp
from ._parse import to_float
from .base import DsoOverlay
from .base import ExtractorError
from .base import brussels_sibelga_overlay
from .base import walloon_dso_overlay
import logging
import re
from ..const import DSO_AIEG
from ..const import DSO_AIESH
from ..const import DSO_FLUVIUS_ANTWERPEN
from ..const import DSO_FLUVIUS_HALLE_VILVOORDE
from ..const import DSO_FLUVIUS_IMEWO
from ..const import DSO_FLUVIUS_INTERGEM
from ..const import DSO_FLUVIUS_IVEKA
from ..const import DSO_FLUVIUS_LIMBURG
from ..const import DSO_FLUVIUS_WEST
from ..const import DSO_FLUVIUS_ZENNE_DIJLE
from ..const import DSO_ORES

_LOGGER = logging.getLogger(__name__)


def _three_col_row(text: str, label: str) -> tuple[str, str, str] | None:
    """The three FL / WAL / BX values of one tax row, or ``None``.

    Bolt prints the values inline on the label line on an archived
    pre-redesign card and on the lines below it on a current one:

        old: "Droit d'accise spécial (c€/kwh) (**) 5,0329 5,0329 5,0329"
        now: "Droit d'accise spécial (c€/kwh) (c€/kWh) 5\\n 5,0329\\n 5,0329\\n 5,0329"

    and prefixes them with a bare footnote marker ("5" above) often enough
    that taking the first three numbers reads the marker as the Flanders
    value, billing the excise at 5 c€/kWh. Requiring a decimal separator told
    a value from a marker, but that is the wrong discriminator twice over: a
    levy printed as a whole number is then rejected outright, raising and
    stalling EVERY Bolt contract (Belgium zeroed the federal levy in August
    2026, so this is not hypothetical), and the unbounded skip could run past
    a row whose own values were missing and capture the NEXT row's silently.

    So bound the row instead: read forward from the label until a line opens
    a new one, and take the LAST three numbers found. A leading marker falls
    off the front, whole numbers are fine, and a row that really is missing
    its values yields fewer than three and returns ``None`` for the caller to
    raise on.
    """
    m = re.search(label, text)
    if m is None:
        return None
    # U+2028 survives in some renders; treat it as the line break it is.
    rest = text[m.end() :].replace(" ", "\n")
    row: list[str] = []
    for n, line in enumerate(rest.split("\n")):
        # The first line is the remainder of the label's own line, so a word
        # on it (the unit, "(c€/kWh)") does not open a new row.
        if n and re.match(r"\s*[^\W\d_]", line):
            break
        row.append(line)
    nums = re.findall(r"-?\d+(?:[.,]\d+)?", " ".join(row))
    if len(nums) < 3:
        return None
    return nums[-3], nums[-2], nums[-1]


def _row_is_explicit_zero(text: str, label: str) -> bool:
    """True when the row exists but prints a dash in each of its three columns.

    Belgium abolished the federal energy contribution in August 2026. Bolt did
    not drop the row, it kept the label and replaced the rates with "-", one
    per region, while the excise beside it absorbed the levy.

    A dash is the card SAYING zero. That is a different fact from a row whose
    values could not be read, which is layout drift, and only the first may be
    priced silently: reading a missing row as zero is how a card that changed
    shape bills several c\u20ac/kWh short behind a passing extractor. So this
    answers a narrower question than :func:`_three_col_row` and the caller
    keeps raising for everything else.
    """
    m = re.search(label, text)
    if m is None:
        return False
    rest = text[m.end() :].replace("\u2028", "\n")
    values: list[str] = []
    for n, line in enumerate(rest.split("\n")):
        # Skip the remainder of the label's own line: it carries the unit
        # ("(c\u20ac/kWh)"), which is neither a value nor a new row.
        if n == 0:
            continue
        if re.match(r"\s*[^\W\d_]", line):
            break
        token = line.strip()
        if token:
            values.append(token)
    return len(values) >= 3 and all(v in {"-", "\u2013", "\u2014"} for v in values)


def _extract_taxes(text: str, region: str) -> tuple[float, float, float]:
    """Return (federal_excise, energy_contribution, region_connection_fee).

    Bolt prints taxes as 3-column rows (Flandres / Wallonie / Bruxelles).
    Caller (parse_snapshot) already normalised any Unicode line
    separators (U+2028) to regular newlines, so the regexes below
    see a uniform layout.
    """
    # Both rows are read by _three_col_row, which bounds the row and takes the
    # last three numbers in it: see there for why a leading footnote marker,
    # a whole-number levy and a row missing its values all have to work.
    excise_row = _three_col_row(text, r"Droit d['’]accise spécial")
    contribution_row = _three_col_row(text, r"Contribution sur l['’]énergie")
    # Federal excise and energy contribution are mandatory federal levies
    # printed on every Belgian supplier card; a regex miss means the
    # layout drifted and the snapshot would silently under-bill by
    # several c€/kWh. Raise so the coordinator falls back to the cached
    # snapshot instead.
    if excise_row is None:
        raise ExtractorError("Bolt: 'Droit d'accise spécial' row not found")
    if contribution_row is None:
        # Scoped to THIS row on purpose. The energy contribution is the levy
        # that was actually abolished, so a dash there is a rate; the excise
        # above is never zero, so the same tolerance would turn a real layout
        # change into a silent under-bill.
        if not _row_is_explicit_zero(text, r"Contribution sur l['’]énergie"):
            raise ExtractorError("Bolt: 'Contribution sur l'énergie' row not found")
        contribution_row = ("0", "0", "0")
    # Connection fee row prints footnote refs ahead of the values, as bare
    # integers on a current card and as parenthesised stars on an archived
    # pre-redesign one:
    #   now: "Redevance de raccordement (c€/kWh) 6 7 - 0,075 -"
    #   old: "Redevance de raccordement (c€/kWh) (*)(***) - 0,075 -"
    # Allow either form ahead of the values; the three trailing tokens are
    # FL/WAL/BX (some are "-" when not applicable). Capping the eater stops a
    # future card with an integer-only Flanders value from being mistaken for
    # a footnote and silently shifting the columns. Matching bare integers
    # only made every archived month bill Wallonia's connection fee at zero.
    connection_match = re.search(
        r"Redevance de raccordement[^\n]*?\(c€/kWh\)\s*"
        r"(?:(?:\(\*+\)|\d+)\s*){0,4}"
        r"(-|[\d.,]+)\s+(-|[\d.,]+)\s+(-|[\d.,]+)",
        text,
    )

    def _pick(row: tuple[str, str, str] | None, region: str) -> float:
        if row is None:
            return 0.0
        index = {REGION_FLANDERS: 0, REGION_WALLONIA: 1, REGION_BRUSSELS: 2}[region]
        token = row[index].strip()
        if token == "-" or not token:
            return 0.0
        return to_float(token) / 100.0

    excise = _pick(excise_row, region)
    contribution = _pick(contribution_row, region)
    connection = _pick(
        None
        if connection_match is None
        else (
            connection_match.group(1),
            connection_match.group(2),
            connection_match.group(3),
        ),
        region,
    )
    return excise, contribution, connection


def _extract_energy_fund(text: str, *, professional: bool = False) -> float:
    """Flemish energy fund in EUR/month, from the row this contract bills.

    Every card prints both categories. A domiciled residential connection
    pays the 'résidentiel' row, which is '-' (0); a business connection pays
    the 'non-résidentiel' one, which the same card fills in (10,07 on the
    August 2026 card). Reading the residential row for a professional
    contract dropped the levy entirely.

    The two rows are laid out differently and need their own patterns: the
    residential value sits after a U+2028 that the text layer normalises to
    a newline, while the non-residential values are inline on the label
    line after an optional footnote marker.

    The pre-redesign archive card splits the same information differently
    again: a bare ``Cotisation Fond énergie (€/mois) (*)`` heading, then
    ``Résidentiel`` and ``Non-résidentiel 10,07 - -`` as their own rows. The
    professional editions walk that archive too, so matching only the current
    single-line label billed 0,00 where the card says 10,07: re-opening,
    for those months, the bug the non-residential row was added to fix.
    """
    if professional:
        match = re.search(
            r"Cotisation Fond énergie, non-résidentiel\s*\(€/mois\)\s*"
            r"(?:\d+\s+)?([\d.,-]+)",
            text,
        ) or re.search(
            r"^\s*Non-r[ée]sidentiel\s+([\d.,-]+)",
            text,
            re.MULTILINE,
        )
    else:
        match = re.search(
            r"Cotisation Fond énergie, résidentiel[^\n]*\n\s*([\d.,-]+)",
            text,
        )
    if match is None or match.group(1).strip() == "-":
        return 0.0
    return to_float(match.group(1))


def _extract_renewables(text: str) -> tuple[float, float, float]:
    """Three columns under 'Certificats verts' + Flanders-only WKK row."""
    cert = re.search(
        r"Certificats verts\s*\(c€/kWh\)[^\n]*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)",
        text,
        re.S,
    )
    # WKK row: 'WKK (c€/kWh) 8 0,39 -' - skip the optional (multi-digit)
    # footnote ref before capturing the Flanders value. Require a real
    # whitespace separator so the greedy ``\d*`` can't swallow the
    # leading digits of a multi-digit value when the footnote is absent.
    # The trailing ' -' tokens are placeholders for Wallonia / Brussels.
    #
    # The pre-redesign archive card is French throughout and labels the same
    # row 'Cogénération (c€/kWh)* 0,39 -'. Matching only the Dutch label left
    # the add-on off those months while the certificats row still parsed, so
    # Flanders billed 1,17 instead of 1,56 c€/kWh with nothing raised.
    wkk = re.search(
        r"(?:WKK|Cog[ée]n[ée]ration)\s*\(c€/kWh\)\*?\s+(?:\d+\s+)?([\d.,]+)", text
    )
    if cert is None:
        # Renewables (certificats verts) are charged in every region; a
        # regex miss is a layout drift that would silently zero ~3 c€/kWh.
        raise ExtractorError("Bolt: 'Certificats verts' renewables row not found")
    fl_cents = to_float(cert.group(1))
    wal_cents = to_float(cert.group(2))
    bx_cents = to_float(cert.group(3))
    if wkk is not None:
        fl_cents += to_float(wkk.group(1))
    return fl_cents / 100.0, wal_cents / 100.0, bx_cents / 100.0


def _extract_flanders_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read Fluvius rows. Each has 8 numbers in this order:

      data_mgmt_digital | capacity_digital | dist_normal_digital |
      dist_excl_digital | terme_fixe_classic | dist_normal_classic |
      dist_excl_classic | prosumer

    We bill the digital (SMR3) block - columns 1-4 plus the prosumer
    column - and ignore the trailing classic columns.

    pdfplumber sometimes splits the row vertically (one number per line);
    ``\\s+`` matches any whitespace incl newlines, so a single regex
    handles both layouts.
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _FLANDERS_LABELS.items():
        match = re.search(
            rf"{re.escape(label)}\s+"
            + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
            + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)",
            text,
        )
        if not match:
            continue
        data_mgmt = to_float(match.group(1))
        capacity = to_float(match.group(2))
        dist_normal = to_float(match.group(3))
        dist_excl = to_float(match.group(4))
        prosumer = to_float(match.group(8))
        out[key] = DsoOverlay(
            distribution_single=dist_normal / 100.0,
            # Group 4 is the dedicated exclusive-night meter rate, lower
            # than the normal digital distribution; bill a night circuit
            # at it instead of falling back to the day rate.
            distribution_exclusive_night=dist_excl / 100.0,
            transport=0.0,
            data_management_per_year=data_mgmt,
            capacity_eur_per_kw_year=capacity,
            prosumer_eur_per_kva_year=prosumer,
        )
    return out


def _extract_wallonia_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read Wallonia rows. Each has 10 numbers:

    mono | jour | nuit | excl_nuit | PIC | MEDIUM | ECO | transport |
    terme_fixe (€/an) | prosumer (€/kVA/an)
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _WALLONIA_LABELS.items():
        match = re.search(
            rf"{re.escape(label)}\s+"
            + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
            + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
            + r"([\d.,]+)\s+([\d.,]+)",
            text,
        )
        if not match:
            continue
        mono = to_float(match.group(1))
        peak = to_float(match.group(2))
        offpeak = to_float(match.group(3))
        excl_night = to_float(match.group(4))
        pic = to_float(match.group(5))
        medium = to_float(match.group(6))
        eco = to_float(match.group(7))
        transport = to_float(match.group(8))
        terme_fixe = to_float(match.group(9))
        prosumer = to_float(match.group(10))
        out[key] = walloon_dso_overlay(
            mono=mono,
            peak=peak,
            offpeak=offpeak,
            excl_night=excl_night,
            pic=pic,
            medium=medium,
            eco=eco,
            transport=transport,
            terme_fixe=terme_fixe,
            prosumer=prosumer,
        )
    # Sanity check: under the swap, RESA's distribution_single must
    # remain strictly cheaper than REW's (regulator pattern that holds
    # for every Walloon tariff card we've parsed). If the inequality
    # ever flips, Bolt almost certainly fixed the upstream layout and
    # our compensating swap now inverts correct values: log a
    # warning so the maintainer can drop the swap from
    # _WALLONIA_LABELS instead of silently mis-billing.
    global _RESA_REW_LOGGED
    resa = out.get(DSO_RESA)
    rew = out.get(DSO_REW)
    if resa is None and rew is None:
        # Both rows missing: regex drift covers the whole table; the
        # rest of the parser will already have raised. Stay quiet
        # here.
        return out
    if resa is None or rew is None:
        # Only one of the two parsed: the more dangerous case for
        # the swap, since the surviving row may now be carrying the
        # other DSO's values without anything else to compare it
        # against. Log at ERROR (once per process) so it surfaces in
        # HA's notification bell without re-ringing on every snapshot
        # refresh.
        if not _RESA_REW_LOGGED:
            parsed = DSO_RESA if resa is not None else DSO_REW
            missing = DSO_REW if resa is not None else DSO_RESA
            _LOGGER.error(
                "Bolt RESA/REW row drift: only %s parsed; the label swap "
                "in _WALLONIA_LABELS may now be inverting %s's values",
                parsed,
                missing,
            )
            _RESA_REW_LOGGED = True
        return out
    if resa.distribution_single >= rew.distribution_single:
        if not _RESA_REW_LOGGED:
            _LOGGER.error(
                "Bolt RESA/REW post-swap invariant tripped "
                "(resa=%.4f rew=%.4f); the upstream PDF may have been "
                "fixed and the label swap in _WALLONIA_LABELS likely "
                "needs to be removed",
                resa.distribution_single,
                rew.distribution_single,
            )
            _RESA_REW_LOGGED = True
    return out


def _extract_brussels_dsos(text: str) -> dict[str, DsoOverlay]:
    """Sibelga row: ``Sibelga 9,96 9,96 7,53 7,53 2,27 14,73 -``.

    Layout: mono | jour | nuit | excl_nuit | transport | terme_fixe | prosumer (-)

    Case-insensitive: the pre-April-2026 archive cards print ``SIBELGA``.
    Matching only the current spelling returned an EMPTY dso map for those
    months, and a Brussels entry's year-to-date then skipped every archived
    month outright (``static_breakdown`` raises ``KeyError`` on a missing DSO
    and the walk treats that as "no rate to apply"), billing Q1 at zero.
    """
    match = re.search(
        r"Sibelga\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
        r"([\d.,]+)\s+([\d.,]+)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return {}
    mono = to_float(match.group(1))
    peak = to_float(match.group(2))
    offpeak = to_float(match.group(3))
    excl_night = to_float(match.group(4))
    transport = to_float(match.group(5))
    terme_fixe = to_float(match.group(6))
    return {
        DSO_SIBELGA: brussels_sibelga_overlay(
            mono=mono,
            peak=peak,
            offpeak=offpeak,
            excl_night=excl_night,
            transport=transport,
            data_management_per_year=terme_fixe,
            osp_by_tier=parse_brussels_osp(text),
        )
    }


_FLANDERS_LABELS: dict[str, str] = {
    "Fluvius Antwerpen": DSO_FLUVIUS_ANTWERPEN,
    "Fluvius Halle-Vilvoorde": DSO_FLUVIUS_HALLE_VILVOORDE,
    "Fluvius Imewo": DSO_FLUVIUS_IMEWO,
    "Fluvius Kempen": DSO_FLUVIUS_IVEKA,
    "Fluvius Limburg": DSO_FLUVIUS_LIMBURG,
    "Fluvius Midden-Vl": DSO_FLUVIUS_INTERGEM,
    "Fluvius West": DSO_FLUVIUS_WEST,
    "Fluvius Zenne-Dijle": DSO_FLUVIUS_ZENNE_DIJLE,
}
_WALLONIA_LABELS: dict[str, str] = {
    "AIEG": DSO_AIEG,
    "AIESH": DSO_AIESH,
    "ORES (Brabant Wallon)": DSO_ORES,
    "TECTEO RESA": DSO_REW,
    "WAVRE": DSO_RESA,
}


# Process-wide latch for the RESA/REW invariant ERROR. Tripped on the
# first occurrence per HA boot and skipped thereafter so a long-lived
# Bolt regression doesn't repeatedly ring HA's notification bell.
_RESA_REW_LOGGED = False

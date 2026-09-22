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

"""Reading a figure off a line of a tariff card.

Belgian cards print numbers in half a dozen ways: a comma decimal, a thousands
separator that looks like a decimal point, a sign that is a word, a rate that
is a column heading. These turn a line of extracted text into a number, or
raise rather than guess. They know nothing about which supplier printed it.
"""

from __future__ import annotations

from ..const import REGION_BRUSSELS, REGION_FLANDERS, REGION_WALLONIA
from .base import DsoOverlay, ExtractorError, TaxOverlay, brussels_sibelga_overlay
from collections.abc import Sequence
from difflib import SequenceMatcher
import re
import unicodedata
from typing import TypeVar


def fold_accents(text: str) -> str:
    """Lowercase and strip Latin diacritics.

    Belgian / French / Dutch tariff PDFs sometimes lose their accents
    when extracted (font / CMap quirks in pypdf), so a literal substring
    test for ``"août"`` misses an extracted ``"aout"``. Provider-side
    cross-checks should fold both haystack and needle through this
    helper to compare apples-to-apples.
    """
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", text.lower())
        if not unicodedata.combining(c)
    )


def parse_prosumer_column(
    section: str, labels: dict[str, str], *, skip_columns: int = 4
) -> dict[str, float]:
    """DSO key -> prosumer EUR/kVA/year, off an analog-meter DSO table.

    The Flemish analog-meter table prints the prosumer forfait as the column
    after ``skip_columns`` numeric ones. Ecofix and EBEM read it with the same
    eight lines; only the section they read it from differs, and that stays at
    the call site along with any per-supplier gate on whether the table exists
    at all.

    A label the table does not carry is simply absent from the result, the same
    as before: the caller falls back to leaving the rate unset.
    """
    out: dict[str, float] = {}
    skip = r"[\d.,]+\s+" * skip_columns
    for label, key in labels.items():
        row = re.search(rf"{re.escape(label)}\s+" + skip + r"([\d.,]+)", section)
        if row:
            out[key] = to_float(row.group(1))
    return out


def require_contract(by_id: dict[str, _T], contract_id: str, label: str) -> _T:
    """The contract definition for ``contract_id``, or raise.

    Fourteen call sites across seven extractors spelled out the same lookup and
    the same "unknown <supplier> contract" message. The guard inside
    ``parse_snapshot`` is NOT redundant with the one in ``fetch``: that function
    is the public entry point the tests and the live check call directly.

    ``fetch_for_month`` keeps its own ``return None`` instead: a month a
    supplier never published has to resolve to None so the caller falls back to
    the current-card proxy, not blow up the year-to-date walk.
    """
    try:
        return by_id[contract_id]
    except KeyError:
        raise ExtractorError(f"unknown {label} contract {contract_id!r}") from None


def to_float(text: str) -> float:
    """Parse a Belgian / French decimal number ('15,93' or '0.102').

    Strips every Unicode space variant Belgian PDFs use as a
    thousands separator or unit padder before swapping the comma
    for a decimal point. Without this, NNBSP-separated values like
    '5 029' raise ValueError mid-page.
    """
    cleaned = text.strip()
    for sep in _NUMERIC_SEPARATORS:
        cleaned = cleaned.replace(sep, "")
    return float(cleaned.replace(",", "."))


def tier_bound_kwh(text: str) -> float:
    """Parse a consumption-tier bound like ``20.000`` or ``1.000.000``.

    Distinct from :func:`to_float`: the dot here is a thousands separator,
    not a decimal point, so ``to_float`` would read 20.000 kWh as twenty
    and band every site into the wrong tranche. Tier bounds are always
    whole kWh, so dropping the separators is exact.
    """
    cleaned = text.strip()
    for sep in _NUMERIC_SEPARATORS:
        cleaned = cleaned.replace(sep, "")
    return float(cleaned.replace(".", ""))


def _row_label(line: str) -> str:
    """A row's leading text, up to where its figures start."""
    found = _ROW_NUMBER.search(line)
    return (line[: found.start()] if found else line).strip().rstrip(":").strip()


def numeric_row(
    text: str,
    label: str,
    columns: int | None = None,
    *,
    after: str | None = None,
    before: str | None = None,
    threshold: float = 0.82,
) -> list[str] | None:
    r"""The figures of the table row whose label reads as ``label``.

    A literal ``label\s+(num)\s+(num)...`` says two things it does not mean.
    That the label is spelled exactly so, when a card rendered as a page image
    and read back drops a character now and then. And, because ``\s`` matches
    a newline, that four figures anywhere after the label will do, including
    four belonging to the row below. Both were measured on Ecofix's Flexy card:
    its consumption row came back as ``Maandprjs``, one letter short, so the
    anchor walked past it to the Injectie block's ``Maandprijs`` and billed a
    household's consumption at 0,0432 where the card says 0,1181, a clean parse
    and a wrong bill.

    So this reads a row the way a person does. The label is matched by
    similarity, not spelling. A row's figures are all on ONE line, because that
    is what a row is; the only line it will look past is a label too long for
    its column, sitting alone above its own figures. The column count, where
    the caller knows it from the card's own headings, has to match. ``after``
    and ``before`` bound the search to the block the row belongs to, which is
    what keeps a consumption lookup away from an injection row carrying the
    same word.

    Returns the figures as they are printed, for :func:`to_float`, or ``None``
    when nothing matches well enough. Refusing is the point: a wrong row is
    worse than no row, and the caller raises where the card promises one.
    """
    body = text
    if after is not None:
        start = body.find(after)
        if start < 0:
            return None
        body = body[start + len(after) :]
    if before is not None:
        end = body.find(before)
        if end >= 0:
            body = body[:end]

    best: tuple[float, list[str]] | None = None
    lines = body.splitlines()
    for index, line in enumerate(lines):
        numbers = _ROW_NUMBER.findall(line)
        if not numbers or (columns is not None and len(numbers) != columns):
            continue
        row_label = _row_label(line)
        if not row_label and index:
            # A label too long for its column wraps onto its own line and
            # leaves the figures alone on the next one: pdfplumber does
            # this to Ecofix's Fluvius West and Zenne-Dijle rows. A person
            # reads those two lines as one row, so take the label from the
            # line above when it carries no figures of its own.
            previous = lines[index - 1]
            if not _ROW_NUMBER.search(previous):
                row_label = _row_label(previous)
        score = SequenceMatcher(None, row_label.lower(), label.lower()).ratio()
        if score >= threshold and (best is None or score > best[0]):
            best = (score, numbers)
    return None if best is None else best[1]


def parse_sign(char: str) -> float:
    """Return -1.0 for any hyphen / dash / Unicode-minus, +1.0 otherwise.

    Use as ``base = parse_sign(m.group(N)) * to_float(m.group(N+1))`` so
    a future card that swaps to U+2212 (or '+' for an indexation that
    flips polarity) doesn't silently break the parser.
    """
    return -1.0 if char in _NEGATIVE_SIGNS else 1.0


def parse_brussels_osp(text: str) -> dict[str, float] | None:
    """Parse the Brussels Brugel OSP annual-fee table off a Sibelga card.

    Every Brussels card that prints the public-service-obligation block
    lists one flat EUR/year fee per connection-power tier. The supplier
    extractors render it several ways (label above vs. beside the value;
    ``et``/``en``/``Entre``/``<=`` phrasings), but each tier row always ends
    ``<bound> kVA <value>``, so anchor on the value-bearing ``kVA`` token.
    Returns every tier the card prints, keyed by the shared tier ids, or None
    when the block is absent (a card that omits it, or a non-Brussels card).

    The rows have to be told apart by their operator, not by the number alone.
    "> 36 et <= 56 kVA" and "> 56 kVA" both end in ``56 kVA``, so keying on the
    bound would make the open-ended top row overwrite the one below it and
    charge a 40 kVA connection the 56-and-above fee.
    """
    # Case-insensitive: Bolt prints "Obligations de service publique" (lower
    # 's'), the other French cards "Obligations de Service Public". Brussels
    # is bilingual and the Dutch cards head the same table "Taks openbare
    # dienstverplichtingen (ODV)", so both languages anchor here rather than
    # the block going unread on a card that prints it.
    block = re.search(
        r"(?:Obligations de Service|Taks openbare dienstverplichtingen)"
        r".*?(?=\n\s*\(\d\)|\Z)",
        text,
        re.S | re.I,
    )
    if block is None:
        return None
    out: dict[str, float] = {}
    for match in re.finditer(
        r"(?P<open>>\s*)?(?P<bound>[\d.,]+)\s*kVA[\s\n]+(?P<value>[\d.,]+)",
        block.group(0),
    ):
        bound = round(to_float(match.group("bound")), 2)
        if match.group("open"):
            # A ">" immediately before the value-bearing bound is the
            # open-ended top row, in both layouts. A closed row reaches this
            # bound through "<=" (Engie, TotalEnergies) or "et" (Mega), so its
            # own ">" sits in front of the LOWER bound and never here.
            tier = _OSP_OPEN_TIER if bound >= _OSP_OPEN_MIN_BOUND else None
        else:
            tier = _OSP_BOUND_TO_TIER.get(bound)
        if tier is not None:
            out[tier] = to_float(match.group("value"))
    return out or None


def parse_sibelga_row(text: str) -> DsoOverlay | None:
    """Build the Brussels overlay from the eight-column Sibelga row.

    Layout, which Engie's French card and EnergyVision's Dutch Brusol card
    print identically:

        distribution Normal | Pleines | Creuses | Excl Nuit (c€/kWh) |
        metering (€/an) | power term <=13kVA (€/an) |
        power term >13kVA (€/an) | Transport (c€/kWh)

    Returns None when the row is absent, leaving the caller to decide
    whether that is a card without a Brussels table or a drift to refuse.

    Not every Brussels card uses this layout: TotalEnergies prints seven
    columns and puts the power term on its own "Terme de puissance mise a
    disposition" line, so it keeps its own reader rather than being bent
    into this one.
    """
    row = numeric_row(text, "SIBELGA", 8)
    if not row:
        return None
    nums = [to_float(value) for value in row]
    # A residential <=13kVA Brussels connection is billed both the metering
    # fee (nums[4]) and the Sibelga <=13kVA power term (nums[5]). Brussels
    # has no separate capacity charge (capacity is Flanders-only), so fold
    # both flat annual euros into the DSO fee.
    return brussels_sibelga_overlay(
        mono=nums[0],
        peak=nums[1],
        offpeak=nums[2],
        excl_night=nums[3],
        transport=nums[7],
        # Columns 5 and 6 are the power term's two bands, at or below
        # 13 kVA and above it; the metering fee (4) is billed either way.
        data_management_per_year=nums[4] + nums[5],
        power_term_above_13kva=nums[4] + nums[6],
        osp_by_tier=parse_brussels_osp(text),
    )


def parse_vreg_network_ceiling(text: str) -> float | None:
    """The VREG maximumtarief a Flemish card states, in EUR/kWh.

    The regulator caps what a digital-meter connection pays in network
    charges: the capacity term plus the per-kWh network term together may not
    exceed this times the volume. ``fees._capped_capacity_annual`` applies it
    and several cards print it as a column of their DSO table, which those
    extractors read. Luminus and Frank state it once in a footnote instead,
    and it went unread there, so the cap never bound on 15 contracts. It bites
    where the capacity term dominates, which is a low-volume connection on a
    high peak.

    ``None`` when the card does not state it, which leaves the cap off as
    before rather than inventing a ceiling.
    """
    match = _VREG_CEILING_RE.search(re.sub(r"\s+", " ", text))
    return to_float(match.group(1)) if match else None


def regional_tax_overlay(
    text: str,
    *,
    supplier: str,
    region: str,
    excise: Sequence[re.Pattern[str]],
    renewables: Sequence[re.Pattern[str]],
    contribution: re.Pattern[str] | None = None,
    fund: re.Pattern[str] | None = None,
) -> TaxOverlay:
    """The tax block of a single-region, VAT-inclusive card.

    ``region`` decides which of the three regional renewables fields the
    parsed levy lands in; the other two stay zero, which is the honest value
    on a card that prices one region.

    Every such card carries the same four rows and the same policy about
    which of them may be missing. Only the anchors differ, so the callers pass
    compiled patterns and this holds the policy:

    * ``excise``: MANDATORY. Patterns are tried in order and the first match
      wins, so a card printing both the flat August-2026 row and the tiered
      one being phased out resolves to the flat rate.
    * ``renewables``: MANDATORY, and ALL of them must match. Summed. Some
      cards print GSC and WKK separately, others one pre-summed row, and a
      Brussels card prints a single "groene stroom" cost.
    * ``contribution``: OPTIONAL, absent means 0.0. The federal levy dropped
      to zero on 2026-08-01 and suppliers answered by deleting the row, so an
      absent row is the abolished levy, not a layout drift.
    * ``fund``: OPTIONAL, absent means 0.0, and it is EUR/month so it is NOT
      scaled by 100 like the c€/kWh rows.

    Sharing the policy is the point. It was written out three times and had
    already drifted: two suppliers defaulted an absent contribution row to
    zero while the third still raised on it, so that one would have gone
    offline the moment its card dropped the row like the others' did.
    """
    if region not in (REGION_FLANDERS, REGION_WALLONIA, REGION_BRUSSELS):
        # Silently zeroing all three fields would under-bill by the whole
        # green levy, so a region that names no field is a programming error.
        raise ExtractorError(f"{supplier}: unknown region {region!r}")
    excise_match = next((m for p in excise if (m := p.search(text))), None)
    if excise_match is None:
        raise ExtractorError(f"{supplier}: could not parse the tax block")
    renewables_matches = [p.search(text) for p in renewables]
    if not renewables or any(m is None for m in renewables_matches):
        # These cards always bill a green-certificate levy; a miss is a layout
        # drift that would silently under-bill, so fail loud and let the
        # coordinator keep serving its cached snapshot.
        raise ExtractorError(f"{supplier}: could not parse the GSC/WKK levies")
    contribution_match = contribution.search(text) if contribution else None
    fund_match = fund.search(text) if fund else None
    levy = sum(
        to_float(m.group(1)) / 100.0 for m in renewables_matches if m is not None
    )
    return TaxOverlay(
        federal_excise=to_float(excise_match.group(1)) / 100.0,
        energy_contribution=(
            to_float(contribution_match.group(1)) / 100.0 if contribution_match else 0.0
        ),
        flanders_renewables=levy if region == REGION_FLANDERS else 0.0,
        wallonia_renewables=levy if region == REGION_WALLONIA else 0.0,
        brussels_renewables=levy if region == REGION_BRUSSELS else 0.0,
        energy_fund_eur_per_month=(
            to_float(fund_match.group(1)) if fund_match else 0.0
        ),
        # These cards print every value VAT-inclusive (the federal excise and
        # the energy fund are VAT-exempt), so the snapshot needs no gross-up.
        vat_rate=0.0,
    )


_T = TypeVar("_T")
_NUMERIC_SEPARATORS = (
    " ",  # ASCII space
    " ",  # NBSP (U+00A0)
    " ",  # THIN SPACE (U+2009)
    " ",  # NARROW NO-BREAK SPACE (U+202F, CLDR French thousands)
    " ",  # LINE SEPARATOR (U+2028)
)
# Single source of truth for the sign character that appears between
# BELPEX/Epex factor and base across every supplier formula (both
# consumption and injection sides). Hyphen-minus, ASCII plus,
# figure-dash, en-dash, em-dash, and U+2212 mathematical minus are
# all encountered in the wild; supplier PDFs flip silently between
# them on re-renders.
_NEGATIVE_SIGNS = ("-", "‐", "‑", "‒", "–", "—", "−")
# One capture group around a plain decimal, with NO thousands separator.
# Named for that constraint on purpose: a card whose values run into four
# digits needs eneco's wider pattern instead, and reusing this one there
# truncates the value to its first digits (recorded beside eneco's _NUM).
# A Belgian figure carries a thousands separator AND a decimal comma, so a
# pattern allowing only one of them reads 1.234,56 as two columns and makes
# a row look wider than it is. The cards print 20.000 and 1.000.000 (see
# tier_bound_kwh), so this is not hypothetical.
_ROW_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
# Residential connection-power upper bounds (kVA) mapped to the OSP tier keys
# shared with const.CONNECTION_KVA_TIER_*. Kept as literals so this low-level
# helper stays decoupled from the config module; the keys must match.
_OSP_BOUND_TO_TIER: dict[float, str] = {
    1.44: "le1_44",
    6.0: "le6",
    9.6: "le9_6",
    13.0: "le13",
    18.0: "le18",
    36.0: "le36",
    56.0: "le56",
}
# The table's last row has no upper bound, so it is keyed by the bound it opens
# at rather than by one it closes. Suppliers write that bound as the top of the
# band below ("> 56 kVA") or as the first value above it ("> 56,01 kVA"), so
# the check is "at least", not equality.
_OSP_OPEN_TIER = "gt56"
_OSP_OPEN_MIN_BOUND = 56.0
# "Un tarif maximal de 0,3472738 €/kWh (hors gestion des données) s'applique
# aux compteurs digitaux", and in Dutch "Voor digitale meters geldt een
# maximumtarief van 0,3472738 EUR/kWh (excl. databeheer)". One VREG figure for
# the whole of Flanders, so a card that states it once states it for every
# Fluvius area on it.
_VREG_CEILING_RE = re.compile(
    r"(?:tarif\s+maximal|maximum\s*tarief)\s+(?:de\s+|van\s+)?"
    r"([\d.]+,\d+|[\d,]+\.\d+)\s*(?:EUR|€)\s*/\s*kWh",
    re.IGNORECASE,
)


SIGN_CHARS = r"+\-‐‑‒–—−"
"""Drop into a regex character class: ``[`` + SIGN_CHARS + ``]``."""

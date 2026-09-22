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

"""The ranking, as a record and as the text a step renders.

One row per candidate, the table they print as, the bar charts beside it and
the notes that qualify them: what the card did not print, what the what-if
overrode, how old the figures are. Kept apart from the arithmetic so a
wording change cannot touch a bill.
"""

from __future__ import annotations

from .const import SOLAR_REGIME_COMPENSATION
from .const import SOLAR_REGIME_INJECTION
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from homeassistant.util import dt as dt_util
from typing import Any


def _populate_charts(
    placeholders: dict[str, str], *, current_label: str, compare_label: str
) -> None:
    """Render the annual / YTD bars from the numeric placeholders.

    Reads the ``current_annual`` / ``compare_annual`` (and YTD pair)
    placeholders and replaces ``annual_chart`` / ``ytd_chart`` with a
    two-row bar visualisation. Leaves them empty when either side is
    "-" so the result page still looks clean for the no-quote-yet
    case (e.g. fetch failed)."""
    for prefix, chart_key in (("annual", "annual_chart"), ("ytd", "ytd_chart")):
        cur = placeholders.get(f"current_{prefix}", "-")
        cmp_ = placeholders.get(f"compare_{prefix}", "-")
        if cur == "-" or cmp_ == "-":
            continue
        try:
            cur_v = float(cur)
            cmp_v = float(cmp_)
        except ValueError:
            continue
        placeholders[chart_key] = _bar_chart(
            ((current_label, cur_v), (compare_label, cmp_v))
        )


def _bar_chart(values: Sequence[tuple[str, float]], width: int = 20) -> str:
    """Two-row unicode bar chart, both rows scaled against the larger
    value so the visual ratio matches the numeric one. Labels are
    padded so the bars line up.

    Takes an ORDERED SEQUENCE of pairs, not a mapping. Keyed by label, two
    sides carrying the same label collapsed into one row - and into the wrong
    one, because the second value overwrote the first while the first label
    survived. Comparing two contracts from one supplier did exactly that, and
    it became the common case once the picker started offering the user's own
    contract.

    Negative-billing cases (a large solar credit) are clamped to zero for the
    bar only; the EUR values still render so the sign stays visible.
    """
    if not values:
        return ""
    max_v = max(max((v for _, v in values), default=0.0), 1.0)
    label_w = max(len(k) for k, _ in values)
    rows: list[str] = []
    for label, v in values:
        bar_v = max(v, 0.0)  # negative annuals (huge solar credit) clamp to empty
        filled = round((bar_v / max_v) * width)
        filled = max(0, min(width, filled))
        bar = "█" * filled + "░" * (width - filled)
        rows.append(f"  {label.ljust(label_w)} {bar} {v:.0f} EUR")
    return "\n".join(rows)


def _row_label(supplier_label: str, contract_label: str) -> str:
    """One row's name, de-duplicated against its supplier.

    Some suppliers put their own name in the product ("Eneco Zon & Wind Vast")
    and some do not ("Fix"), so joining unconditionally reads as "Eneco Eneco
    Zon & Wind Vast" for one half of the table and correctly for the other.

    No truncation. The name used to be elided to a fixed width so columns
    lined up inside a code fence, which cost exactly the tails these names
    disambiguate on - Agilior Online GREEN against Agilior Online. The table
    is wrapping markdown now, so the full name fits however narrow the screen.
    """
    if contract_label.lower().startswith(supplier_label.lower()):
        return contract_label
    return f"{supplier_label} {contract_label}"


@dataclass(frozen=True)
class RankedRow:
    """One line of the ranking, already priced or explicitly not.

    ``annual`` is None when the row could not be priced, and ``status`` says
    why in the household's own terms. A row that failed is not dropped: a
    missing row reads as "not competitive", which is the one thing it does not
    mean.
    """

    label: str
    annual: float | None
    ytd: float | None = None
    status: str = ""
    is_own: bool = False
    # Priced off the card archive's OCR reading, because this supplier
    # publishes its card as page images and no parser can read one. A price
    # rather than no price, but a reading, and the row says so: a figure you
    # might switch supplier over must not hide where it came from.
    read_by_ocr: bool = False


@dataclass(frozen=True)
class DailyCompare:
    """The result of one scheduled ranking, as the sensor publishes it.

    Holds the rows rather than a rendered table: the sensor exposes numbers
    for automations to read and the dialog renders the same rows through
    ``_ranking_table``, and formatting it here would give the two different
    answers to the same question.

    ``own`` is None on a cold entry whose own card has not resolved yet. The
    ranking is still worth publishing then: the alternatives rank against
    each other, but there is no saving to state, so the sensor reads unknown
    rather than claiming zero.
    """

    rows: tuple[RankedRow, ...]
    own: float | None
    priced: int
    total: int
    ran_at: datetime

    @property
    def cheapest(self) -> RankedRow | None:
        """The best-priced alternative, or None if nothing priced.

        Excludes the household's own row: "the cheapest contract available to
        you" is a question about the alternatives, and answering it with your
        own contract when yours happens to win would report a saving of zero
        against itself.
        """
        priced = [r for r in self.rows if r.annual is not None and not r.is_own]
        if not priced:
            return None
        return min(priced, key=lambda r: r.annual if r.annual is not None else 0.0)

    @property
    def saving(self) -> float | None:
        """Yearly euro the cheapest alternative would save, or None.

        Negative is a real answer and is left signed: it means nothing on the
        market beats what the household already has, which is what somebody
        checking a comparison sensor most wants to be told.
        """
        best = self.cheapest
        if best is None or best.annual is None or self.own is None:
            return None
        return self.own - best.annual


def _eur(value: float) -> str:
    """A euro amount in the Belgian convention, comma for the decimal."""
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _ranking_table(
    rows: Sequence[RankedRow],
    *,
    deferred: int = 0,
    ran_at: datetime | None = None,
) -> str:
    """The ranking, as wrapping markdown rather than an aligned block.

    It was a fixed-width table inside a code fence, which a Home Assistant
    dialog renders monospace and never wraps: 68 columns meant scrolling
    sideways to read a row, which on a phone makes the page unusable. Markdown
    reflows to the dialog, so the same rows are readable at any width and the
    contract names no longer have to be elided to keep columns aligned.

    The price leads each line so it survives a wrap: a long name pushes to the
    next line, the figure being compared does not.

    No emphasis markers anywhere in a row. Bold and italics were reported
    rendering as literal asterisks around the amounts, and a price wearing
    stars reads worse than a plain one either way, so the row carries no
    markup a renderer can leak.

    Rows that priced are sorted and numbered; rows that did not follow
    underneath saying why, because dropping them reads as "not competitive"
    and losing them silently is how a sweep looks complete when it is not.
    """
    priced = sorted(
        (r for r in rows if r.annual is not None),
        key=lambda r: r.annual if r.annual is not None else 0.0,
    )
    unpriced = [r for r in rows if r.annual is None]
    if not priced and not unpriced:
        return ""

    # Every gap is measured against the household's OWN contract, not against
    # the cheapest row. "How much would I save by switching" is the question
    # being asked; "how far is this from the best offer" is a different one
    # the reader can already see from the order. The own row is in the list
    # for the same reason: a ranking that does not show you where you
    # currently sit cannot answer either question.
    own = next((r.annual for r in priced if r.is_own), None)
    out: list[str] = []
    for n, row in enumerate(priced, 1):
        annual = row.annual if row.annual is not None else 0.0
        line = f"{n}. {_eur(annual)} EUR - {row.label}"
        if row.is_own:
            # Inline code, which the dialog paints as a filled band: in a
            # list this long the row you are comparing everything against
            # has to be findable without reading. Two words and no more,
            # because inline code is monospace and does not wrap, which is
            # what made the old fenced table scroll sideways.
            line += " `YOUR CONTRACT`"
        elif own is not None:
            delta = annual - own
            # Signed, and the sign is the point: a minus is money saved.
            line += f" · {'+' if delta > 0 else ''}{_eur(delta)}"
        if row.ytd is not None:
            line += f" · YTD {_eur(row.ytd)}"
        if row.read_by_ocr:
            line += " `OCR`"
        out.append(line)

    if unpriced:
        out.append("")
        for row in unpriced:
            out.append(f"- {row.label} - {row.status}")
    if deferred:
        out.append("")
        out.append(
            f"{deferred} more not priced yet - reopen to finish; "
            "the slowest cards are left for last."
        )
    if ran_at is not None:
        # A stored ranking has to date itself. Tariff cards move about once a
        # month so a night-old table is almost always current, but a reader
        # who just watched a supplier republish needs to know this one did
        # not, and which of the two they are looking at.
        out.append("")
        out.append(
            "Ranked "
            + dt_util.as_local(ran_at).strftime("%d/%m at %H:%M")
            + ". Tick the box to price it again now."
        )
    return "\n".join(out)


_VOLUMES_CLAUSE = (
    " Yearly volumes entered by hand, so the year-to-date rows are left "
    "blank: they replay measured meter history, not the figures typed."
)


def _regime_label(regime: str) -> str:
    """Solar regime in the words the result page and the what-if step use.

    Deliberately not the selector's own translated option: these render
    inside a sentence, where "Compensation regime (Wallonia, certified
    before 2024-01-01, until 2030)" does not fit. Every call site prefixes
    a definite article, so each label has to read after "the".
    """
    if regime == SOLAR_REGIME_COMPENSATION:
        return "compensation regime"
    if regime == SOLAR_REGIME_INJECTION:
        return "injection tariff"
    return "no-solar regime"


def _whatif_note(
    base_note: str,
    *,
    stored_regime: str,
    regime: str,
    baseline_eur: float | None,
    whatif_eur: float | None,
    volumes_typed: bool,
    missing_kva: bool = False,
) -> str:
    """The solar note, prefixed and qualified when a what-if regime is in
    play.

    Returns ``base_note`` untouched when the quote runs on the entry's own
    regime, so an ordinary comparison reads exactly as before.

    The baseline clause exists because, with the regime moving both sides
    together, the printed supplier delta barely shifts, and the number the
    user actually came for: their own contract under the other regime,
    would otherwise appear nowhere. It predates the picker offering the
    user's own contract (e2a52af): picking yourself is now a second route to
    the same answer, and the clause still gives it without the detour.
    """
    if regime == stored_regime:
        # Typed volumes blank the year-to-date rows on their own, without
        # any regime change, so the sentence that explains the blank rows
        # cannot hang off the regime having moved.
        return f"{base_note}{_VOLUMES_CLAUSE}".lstrip() if volumes_typed else base_note
    note = (
        f"what-if: both sides quoted on the {_regime_label(regime)} "
        f"(your entry is on the {_regime_label(stored_regime)})."
    )
    if base_note:
        note += f" {base_note}."
    if baseline_eur is not None and whatif_eur is not None:
        delta = whatif_eur - baseline_eur
        note += (
            f" On your own contract that is {baseline_eur:.2f} EUR/year as "
            f"configured versus {whatif_eur:.2f} EUR/year under the "
            f"{_regime_label(regime)} ({'+' if delta >= 0 else ''}{delta:.2f} "
            "EUR/year)."
        )
    if volumes_typed:
        note += _VOLUMES_CLAUSE
    if missing_kva and regime == SOLAR_REGIME_COMPENSATION:
        # The prosumer fee is billed per kVA of inverter, so an entry that
        # never set one quotes the compensation regime without it. Say so
        # rather than print a figure that is short by a few hundred euros.
        note += (
            " No inverter capacity is set on this entry, so no Walloon "
            "prosumer fee is included and the figure is that much too low."
        )
    return note + " Your entry is unchanged."


def _vintage_note(
    current: Any, current_label: str, other: Any, other_label: str
) -> str:
    """Named when the two sides are priced off cards of different vintages.

    Every supplier transcribes the same regulated tariffs onto its own card,
    so the two sides of a quote share their DSO and federal overlays only for
    as long as both cards were published under the same rules. Measured
    across twelve Flemish cards, suppliers agree to four decimals on the DSO
    tables and to the last digit on the excise: until a regulatory change
    lands, and then a card published either side of it differs by about
    0,0036 EUR/kWh, four times the ordinary spread between suppliers and
    worth around 13 EUR a year at 3500 kWh.

    That gap belongs to the calendar, not to the offer, so it is disclosed
    rather than corrected: re-pricing one side onto the other's overlays
    would invent a card neither supplier published.

    ``valid_until`` is a real date and sorts. ``publication_label`` is free
    text off the card ('Avril 2026', 'augustus 2026', '04/2026') and does
    not, so it is only ever printed, never compared.
    """
    ours = getattr(current, "valid_until", None)
    theirs = getattr(other, "valid_until", None)
    if ours is None or theirs is None or ours == theirs:
        return ""
    if ours < theirs:
        older, older_snap, newer = current_label, current, other_label
    else:
        older, older_snap, newer = other_label, other, current_label
    printed = getattr(older_snap, "publication_label", "") or ""
    stamp = f" ({printed})" if printed else ""
    return (
        f"{older}'s card{stamp} is older than {newer}'s, so each side carries "
        "the regulated tariffs as they stood when it was published; a levy "
        "change between the two moves a side for a reason that is not its offer"
    )


def _card_caveats(snapshot: Any, label: str, *, read_by_ocr: bool = False) -> list[str]:
    """What one side's card does not say, in the household's own terms.

    Separate from ``_uncredited_note``, which explains a missing injection
    credit. These are caveats about the card itself: a regulated charge it
    does not print, so the estimate beside it is short by a real amount the
    household still pays.

    Returned per side and joined by the caller, so the page names which
    supplier each caveat belongs to rather than hedging the whole quote.
    """
    out: list[str] = []
    if read_by_ocr:
        out.append(
            f"{label} publishes its card as page images, so this quote is "
            "priced off the archive's OCR reading of it"
        )
    # A card whose rate IS the delivery month's index prints one computed from
    # the PREVIOUS month's and says so, worth 8,1% under in May and 15,4% over
    # in February on the energy leg of the 2026 cards. Only the entry's own
    # side is ever re-resolved against the current month, and only when it has
    # an ENTSO-E key: _cohort_energy_leg returns None for any other contract,
    # by design, since an alternative has no signing history to price at. The
    # refreshed leg comes back as SpotMonthlyRates and so carries no
    # month_indexed flag, which is what keeps this off a side that did get it.
    if getattr(getattr(snapshot, "energy", None), "month_indexed", False):
        out.append(
            f"{label}'s rate is the one printed on its card, computed on last "
            "month's index"
        )
    taxes = getattr(snapshot, "taxes", None)
    if taxes is not None and getattr(taxes, "region_connection_fee_unavailable", False):
        # The coordinator raises a repair issue for this on the user's own
        # entry, but that says nothing about a target they are being quoted.
        # Wallonia levies the fee and the supplier passes it through, so a
        # card that omits the row bills short and ranks cheaper for a reason
        # that is not its offer.
        out.append(
            f"{label}'s card prints no Walloon connection-fee row, so its "
            "estimate excludes a charge you would still pay"
        )
    return out


def _uncredited_note(snapshot: Any, label: str) -> str:
    """Why one side of the quote credits nothing for the injected kWh.

    ``_compare_injection_credit`` returns None for two different reasons:
    the card publishes no injection tariff at all, or a spot-indexed
    injection had no day-ahead window to price against. The first is the
    true bill with that supplier; the second understates the credit. Both
    fall through to the no-credit branch of ``_annual_bill``, so without a
    note on the page the two are indistinguishable from a supplier that
    genuinely pays nothing.
    """
    if getattr(snapshot, "injection", None) is None:
        return f"{label} publishes no injection tariff, so nothing is credited there"
    return (
        f"{label}'s injection is spot-indexed and no day-ahead price was "
        "available, so nothing is credited there"
    )


def _solar_note(
    regime: str, rolling_inj_kwh: float, uncredited: Sequence[str] = ()
) -> str:
    """One-line description of how solar is folded into the comparison.

    Renders into the result form's description placeholder. Empty for
    the no-solar case so the page doesn't show a misleading label.
    ``uncredited`` carries one clause per side whose injection could not
    be priced, so the page never claims a credit it did not apply."""
    if regime == "compensation":
        if rolling_inj_kwh > 0:
            return f"compensation regime: meter netted (consumption -= {rolling_inj_kwh:.0f} kWh, surplus forfeited)"
        return "compensation regime configured but no injection sensor wired - net = consumption"
    if regime == "injection":
        if rolling_inj_kwh > 0:
            note = f"injection regime: {rolling_inj_kwh:.0f} kWh credited at each supplier's injection price"
            for reason in uncredited:
                note += f" - {reason}"
            return note
        return "injection regime configured but no injection sensor wired - no injection credit applied"
    return ""

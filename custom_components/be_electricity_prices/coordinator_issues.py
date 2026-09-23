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

"""The Repairs issues this entry raises and clears.

Split out of coordinator.py. A pure reader: it reads _last_error, _snapshot,
_unloaded, entry and hass, and writes nothing.

Every issue id is f"{translation_key}_{entry_id}" and must stay byte-identical:
Repairs persists it, so a changed id orphans an already-raised issue with no
way for the user to clear it."""

from __future__ import annotations

from .brugel import any_cached_power_term, cached_power_term
from .providers import get as get_extractor, offers_direct_debit

from .providers.base import ExtractorError, SupplierExtractor, SupplierSnapshot
from .providers._resolve import omits_brussels_power_term

from .const import (
    CONF_CONTRACT,
    CONF_DIRECT_DEBIT,
    CONF_DSO,
    REGION_BRUSSELS,
    CONF_DSO_TARIFF_MODE,
    CONF_METER,
    CONF_REGION,
    CONF_SUPPLIER,
    DOMAIN,
    DSO_MODE_IMPACT,
    METER_EXCLUSIVE_NIGHT,
)
from .fees import (
    _compensation_kva,
)
from .snapshot_store import (
    SNAPSHOT_STALE_DAYS,
)

from datetime import datetime
from typing import TYPE_CHECKING
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util


def _successor_for(supplier_id: str | None, region: str) -> SupplierExtractor | None:
    """The successor supplier, but only when it can serve ``region``.

    A withdrawal announcement names one successor for the whole country,
    while our coverage is per region. Returns ``None`` when the successor is
    unset, unknown to this build, or has no contract in the region, so the
    caller can avoid telling a user to pick a supplier the config flow would
    then refuse.

    Having a contract in the region is not the same as having THEIR contract,
    and this cannot tell the two apart: a withdrawal names a supplier, not the
    product each customer lands on. EnergyVision has five Flemish products and
    the DATS 24 customers who transferred to it landed on none of them, but on
    the legacy card continued under its name, which is published nowhere this
    can read (issue #100). So the card names the successor and then says what
    to do when the product is missing, rather than promising a match it cannot
    check.
    """
    if not supplier_id:
        return None
    try:
        successor = get_extractor(supplier_id)
    except ExtractorError:
        return None
    if not any(region in c.regions for c in successor.contracts):
        return None
    return successor


class _IssuesMixin:
    """Mixed into BePricesCoordinator."""

    # Entry-owned state, declared as BARE annotations with no value. A valued
    # class attribute would change hasattr() and instance-dict behaviour;
    # __init__ in the concrete class is what actually creates these.
    entry: ConfigEntry
    _unloaded: bool
    _snapshot: SupplierSnapshot | None
    _snapshot_raw: SupplierSnapshot | None
    _snapshot_fetched_at: datetime | None
    _snapshot_probe_key: str | None
    _last_error: str | None
    _supplier_tuple: tuple[str, str, str]
    _register_pair_fault: str

    if TYPE_CHECKING:
        # Provided by DataUpdateCoordinator and the sibling mixins. Declared
        # for the type checker rather than inherited, so each mixin is checked
        # on its own and BePricesCoordinator's bases say how they compose. The
        # one mixin composed anywhere else is the profiles mixin, which the
        # spots mixin extends.
        hass: HomeAssistant

    def _sync_issue(
        self,
        key: str,
        active: bool,
        *,
        extra: dict[str, str] | None = None,
        severity: ir.IssueSeverity = ir.IssueSeverity.WARNING,
    ) -> None:
        """Raise or clear one Repairs issue for this entry.

        Five syncers spelled this out: the unloaded guard, the
        ``f"{translation_key}_{entry_id}"`` id, the create call with its
        supplier / contract placeholders, and the delete in the else. Only the
        key, the predicate and a couple of extra placeholders differ.

        The id shape is load-bearing and must stay byte-identical: Repairs
        persists it, so a changed id leaves an already-raised issue orphaned
        with no way for the user to clear it.
        """
        if self._unloaded:
            return
        issue_id = f"{key}_{self.entry.entry_id}"
        if not active:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        placeholders = {
            "supplier": str(self.entry.data.get(CONF_SUPPLIER, "")),
            "contract": str(self.entry.data.get(CONF_CONTRACT, "")),
        }
        placeholders.update(extra or {})
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=severity,
            translation_key=key,
            translation_placeholders=placeholders,
        )

    def _sync_stale_issue(self, stale: bool) -> None:
        """Raise or clear the 'snapshot stale' repair issue for this entry."""
        self._sync_issue(
            "snapshot_stale",
            stale,
            extra={
                "days": str(SNAPSHOT_STALE_DAYS),
                "last_error": self._last_error or "unknown",
            },
        )

    def _sync_exclusive_night_gap_issue(self) -> None:
        """Flag an exclusive-night meter whose DSO overlay cannot price it.

        ``network_eur_per_kwh`` bills an exclusive-night circuit at its own
        distribution rate, falling back to off-peak and then to the single
        (day) rate. When a supplier's card publishes neither, that last
        fallback silently bills the dedicated night circuit at the day rate.
        TotalEnergies' Flemish card is the case: its DSO table prints
        digital/classic prelevement and capacitaire, metering, cotisation,
        transport and prosumer, with no exclusive-night column at all - even
        though it does publish an exclusive-night ENERGY rate, so the entry
        looks fully configured.

        The rate cannot be substituted from anywhere: no EUR value may live
        in Python source, and borrowing another supplier's Fluvius figure
        would be a guess. So price it as the engine already does and tell the
        user, rather than hiding the meter type or silently over-billing.
        """
        overlay = (
            self._snapshot.dsos.get(self.entry.data.get(CONF_DSO, ""))
            if self._snapshot is not None
            else None
        )
        gap = (
            self.entry.data.get(CONF_METER) == METER_EXCLUSIVE_NIGHT
            and overlay is not None
            and overlay.distribution_exclusive_night is None
            and overlay.distribution_offpeak is None
        )
        self._sync_issue(
            "exclusive_night_rate_missing",
            gap,
            extra={"dso": str(self.entry.data.get(CONF_DSO, ""))},
        )

    def _sync_impact_gap_issue(self) -> None:
        """Flag an Impact DSO mode the supplier's card cannot price.

        Ten Walloon contracts omit the CWaPE Tarif Impact block: Cociter
        Tarif Variable and nine Luminus products, all of Luminus' Wallonia
        range but the dynamic card, which is the one that does print it. On
        those the overlay's pic / medium / eco stay None. ``network_eur_per_kwh`` then
        falls back to the bi-horaire branch while ``_routed_rate`` keeps
        routing the ENERGY side through ``dso_impact_band``. The two schedules
        agree for most of the day but not between 22:00 and 01:00, where the
        Impact MEDIUM band bills the peak energy rate against an off-peak
        distribution rate.

        The bill stays close (this is a band mismatch, not the mono-rate
        fallback it looks like from the overlay alone: the static cards do
        publish peak / offpeak). Still worth telling the user, since they
        explicitly opted into Impact and are not being billed on it.
        """
        overlay = (
            self._snapshot.dsos.get(self.entry.data.get(CONF_DSO, ""))
            if self._snapshot is not None
            else None
        )
        gap = (
            self.entry.data.get(CONF_DSO_TARIFF_MODE) == DSO_MODE_IMPACT
            and overlay is not None
            and overlay.distribution_pic is None
        )
        self._sync_issue(
            "impact_rates_missing",
            gap,
            extra={"dso": str(self.entry.data.get(CONF_DSO, ""))},
        )

    def _sync_connection_fee_issue(self) -> None:
        """Flag a Walloon card that stopped printing the connection fee.

        EnergyVision deleted the row from every one of its Walloon cards on
        1 August 2026, together with the energy contribution that really was
        abolished that day. The connection fee was not: Wallonia still levies
        it and the card's own terms keep taxes and redevances fully passed
        through to the customer.

        The extractor bills 0 for it rather than failing the fetch, which
        would leave the entry frozen on a July snapshot still carrying the
        abolished contribution and the superseded excise, and be the larger
        error of the two. Say what the cost excludes so the gap is disclosed
        rather than silent, and clear it the moment the row comes back.
        """
        self._sync_issue(
            "connection_fee_missing",
            self._snapshot is not None
            and self._snapshot.taxes.region_connection_fee_unavailable,
        )

    def _sync_register_pair_issue(self) -> None:
        """Flag a day/night register that stopped, or never recorded.

        A pair is billed only on the days both halves report, and not at all
        while one records nothing, so a register that went silent after a
        rename, an integration swap or a meter replacement leaves the running
        cost short without any error: the figure is simply lower, and the
        coverage attributes are the only other trace. The log says which
        sensor, once a day, where few look; this says it where Home Assistant
        collects what needs fixing, and clears the moment the pair is whole.
        """
        self._sync_issue(
            "register_pair_incomplete",
            bool(self._register_pair_fault),
            extra={"entities": self._register_pair_fault},
        )

    def _sync_prosumer_gap_issue(self) -> None:
        """Flag a compensation install whose card omits the prosumer tariff.

        Cociter's trihoraire card prints the supplier's own 37,10 EUR/kVA
        forfait "en regime de compensation" but drops the "Tarif prosumer"
        column its variable card carries, so the DSO half of the fee is absent
        rather than zero. The tariff is regulated and identical whichever
        supplier reprints it: TotalEnergies publishes it on the same Walloon
        row as the PIC / MEDIUM / ECO bands, which is what rules out reading
        the omission as "the incitative configuration abolishes it".

        ``_prosumer_monthly_fee`` contributes 0 for a missing rate, so the
        entry silently under-bills by the DSO tariff alone: 81 to 99 EUR per
        kVA per year across the five Walloon GRDs, which is 405 to 496 EUR a
        year on a 5 kVA inverter. Say what the cost excludes rather than
        invent a figure from another supplier's card, and clear it the moment
        the column comes back.

        Gated through ``_compensation_kva`` so the eligibility rule stays in
        the one place that owns it: Wallonia, the compensation regime, and a
        kVA that parses above zero.
        """
        overlay = (
            self._snapshot.dsos.get(str(self.entry.data.get(CONF_DSO, "")))
            if self._snapshot is not None
            else None
        )
        self._sync_issue(
            "prosumer_tariff_missing",
            _compensation_kva(self.entry) > 0.0
            and overlay is not None
            and overlay.prosumer_eur_per_kva_year is None,
            extra={"dso": str(self.entry.data.get(CONF_DSO, ""))},
        )

    def _entry_extractor(self) -> SupplierExtractor | None:
        """This entry's registry extractor, or None if this build drops it."""
        try:
            return get_extractor(str(self.entry.data.get(CONF_SUPPLIER, "")))
        except ExtractorError:
            return None

    def _supply_ended(self) -> bool:
        """True once this entry's supplier has stopped supplying.

        The ONE place in the deprecation handling that reads the clock, and
        deliberately so. ``deprecated_until`` is the date the contracts stop
        being supplied, so past it a failing fetch is the expected outcome
        rather than a fault: the supplier simply is not publishing any more.

        Local date, not UTC: the withdrawal is a Belgian calendar event, and
        a UTC comparison flips a day late for CET/CEST users.
        """
        extractor = self._entry_extractor()
        if extractor is None or extractor.deprecated_until is None:
            return False
        return dt_util.now().date() > extractor.deprecated_until

    def _sync_brussels_power_term_issue(self) -> None:
        """Flag a Brussels entry billing without Sibelga's power term.

        Sibelga's fixed charge has a metering part and a power part. Four
        suppliers print the sum and the band above 13 kVA; Bolt prints the
        metering half alone, so ``resolve_brussels_power_term`` completes it
        from the figure Brugel publishes. When that sheet cannot be read the
        card is billed as printed, about 50,07 EUR a year short.

        Asks the resolver's own rule rather than half of it. This keyed on the
        band alone, and the resolver takes TWO signals: no band, AND a metering
        figure too small to already include the power part. A card printing one
        combined number and no band is correctly priced and was being told it
        was 50 EUR a year short. No such card is in the 349 archived Brussels
        rows, which is what the first version of this leaned on, but the
        resolver's second signal exists because that shape is anticipated.

        The comparator is Brugel's own published figure, not a guess about how
        big a metering charge should be: the delivery year's where we have it,
        and otherwise the most recent year we hold, because the term is set per
        calendar year and moves by a few percent where the gap between a
        metering figure and a complete one is fourfold. With no figure at all
        this stays silent, which is the honest answer to a question nothing can
        settle, and which means a process whose very first Brugel fetch failed
        says nothing until one succeeds. That window is the one where the term
        has never been seen at all; the fetch retries every six hours and logs
        its own warning meanwhile.

        Says what the cost excludes rather than inventing it, like the four
        sibling gaps, and clears the moment Brugel answers, which the tick
        retries every six hours.
        """
        if self.entry.data.get(CONF_REGION) != REGION_BRUSSELS:
            self._sync_issue("brussels_power_term_missing", False)
            return
        year = dt_util.now().year
        terms = cached_power_term(year) or any_cached_power_term()
        self._sync_issue(
            "brussels_power_term_missing",
            self._snapshot is not None
            and omits_brussels_power_term(self._snapshot, terms=terms),
        )

    def _sync_direct_debit_unanswered_issue(self) -> None:
        """Flag an entry on a card that prices direct debit but was never asked.

        Four Mega cards grant the WHOLE ristourne only to a direct-debit
        payer, and ``resolve_direct_debit`` clears the base, the per-kWh leg
        and the cap when the answer is no. The answer is read as
        ``entry.data.get(CONF_DIRECT_DEBIT, DEFAULT_DIRECT_DEBIT)``, which
        cannot tell a household that answered no from one that was never
        asked, and an entry created before the question existed has no stored
        answer at all. Such an entry bills nothing where 0.27.0 billed the
        whole credit, 522,58 EUR a year on Cosy Flex at 3500 kWh.

        The conservative reading is the right one for the bill: the card grants
        the credit to a direct-debit payer and this integration does not know
        how the household pays, so inventing the answer either way would bill
        a figure the card does not support. What was wrong is that it happened
        in silence, which is what this card fixes. One tick of the options
        flow settles it for good, and the notice clears the moment an answer
        of either value is stored.

        Raised for all eighteen products the question is offered on, not only
        the four exclusive ones: on the fourteen that state a supplement the
        missing answer costs a payer that supplement rather than the whole
        credit, which is smaller and still wrong.
        """
        self._sync_issue(
            "direct_debit_unanswered",
            offers_direct_debit(
                str(self.entry.data.get(CONF_SUPPLIER, "")),
                str(self.entry.data.get(CONF_CONTRACT, "")),
            )
            and CONF_DIRECT_DEBIT not in self.entry.data,
        )

    def _sync_extractor_issue(
        self,
        message: str | None,
        *,
        transient: bool = False,
        unreadable: bool = False,
    ) -> None:
        """Raise or clear the supplier-extractor repair issue.

        Two mutually-exclusive flavours share this Repairs slot:

        - actionable (``transient=False``): a parse error, 404 or non-PDF
          payload that will not self-heal. Surfaces the ``extractor_failed``
          card whose advice is "the supplier changed its layout, open a
          GitHub issue".
        - transient (``transient=True``): a network timeout / reset / 5xx /
          anti-bot 403 that a later refresh usually recovers. Surfaces the
          softer ``extractor_unreachable`` card.

        ``unreadable`` takes a third slot instead of the actionable one: the
        card downloaded fine but carries no text layer, so "the supplier
        changed its layout, open a GitHub issue" is advice nobody can act on.
        The caller derives it from the error THIS fetch raised, not from a
        per-supplier flag, so the card stops appearing by itself the moment
        readable cards come back. A transient network error still reports as
        transient, because that one clears by itself too.

        Whichever flavour is raised clears the others so the user never sees
        two at once. ``message`` ``None`` means the latest fetch succeeded
        and clears all of them.
        """
        if self._unloaded:
            return
        failed_id = f"extractor_failed_{self.entry.entry_id}"
        unreachable_id = f"extractor_unreachable_{self.entry.entry_id}"
        unreadable_id = f"extractor_unreadable_{self.entry.entry_id}"
        no_prices_id = f"extractor_unreadable_no_prices_{self.entry.entry_id}"
        all_ids = (failed_id, unreachable_id, unreadable_id, no_prices_id)
        # A supplier past its supply end date has stopped publishing, so the
        # fetch failing is the expected outcome and not news. Reporting it
        # stacks an alarming "could not reach the supplier" card on top of
        # the deprecation card that already explains the situation and says
        # what to do; the user is left to work out they describe one event.
        # The deprecation card carries this state on its own.
        if not message or self._supply_ended():
            for issue_id in all_ids:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        if transient:
            raise_id, translation_key = unreachable_id, "extractor_unreachable"
        elif unreadable and self._snapshot is None:
            # Two different situations wearing one name. An entry with a cached
            # card keeps pricing off it and needs to be told the figures drift;
            # an entry with none has every sensor unavailable and no drift to
            # warn about, so it needs the workaround and nothing else.
            raise_id, translation_key = no_prices_id, "extractor_unreadable_no_prices"
        elif unreadable:
            raise_id, translation_key = unreadable_id, "extractor_unreadable"
        else:
            raise_id, translation_key = failed_id, "extractor_failed"
        for issue_id in all_ids:
            if issue_id != raise_id:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            raise_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=translation_key,
            translation_placeholders={
                "supplier": str(self.entry.data.get(CONF_SUPPLIER, "")),
                "contract": str(self.entry.data.get(CONF_CONTRACT, "")),
                "error": message,
            },
        )

    def _sync_card_read_by_ocr_issue(self, active: bool) -> None:
        """Raise or clear the 'these prices were read off the card' notice.

        The supplier publishes its tariff card as page images, and what is
        being served is the repository's OCR reading of one. That is a price
        rather than no price, so it is not a failure, but it is a reading,
        and the user is entitled to know before they act on the figures.
        Cleared by the first readable card.
        """
        self._sync_issue("card_read_by_ocr", active)

    def _sync_entsoe_auth_issue(self, active: bool, message: str = "") -> None:
        """Raise or clear the 'ENTSO-E rejected the API key' issue.

        Fired only on ``EntsoeAuthError`` (transparency.entsoe.eu
        responded 401), so the user knows the fix is "rotate the token
        in the entry's options" rather than waiting on a transient
        outage. Cleared as soon as a refresh succeeds with a key the
        endpoint accepts.
        """
        self._sync_issue(
            "entsoe_auth_failed",
            active,
            extra={"error": message or "401 Unauthorized"},
            severity=ir.IssueSeverity.ERROR,
        )

    def _sync_deprecated_supplier_issue(self) -> None:
        """Raise or clear the 'this supplier is leaving the market' issue.

        Driven purely by the registry's ``deprecated_until`` /
        ``deprecated_successor`` (``providers/base.py``): the card is an
        instruction to switch supplier, and it stays up for as long as the
        entry points at a supplier that has announced its exit. Whether it
        shows at all never depends on the clock. The end date is compared to
        the clock for one thing only, picking the tense: past it the transfer
        has happened, and a card still saying it will happen on a date now in
        the past, and that nothing is broken yet, would be misinforming the
        one user it is aimed at. Clears by itself when the user re-points the
        entry, and on any release that drops the registry flag.

        Kept separate from the extractor / staleness cards on purpose. Those
        say "the fetch is failing"; this one says "the fetch will keep
        working and then stop, and here is what to do about it". Prices are
        untouched: a user still supplied by DATS 24 in August must still be
        billed August's rates.
        """
        if self._unloaded:
            return
        issue_id = f"supplier_deprecated_{self.entry.entry_id}"
        supplier_id = str(self.entry.data.get(CONF_SUPPLIER, ""))
        try:
            extractor = get_extractor(supplier_id)
        except ExtractorError:
            # An entry on a supplier this build no longer ships: the
            # extractor cards already cover that, nothing to add here.
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        if extractor.deprecated_until is None:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ended = self._supply_ended()
        placeholders = {
            "supplier": extractor.label,
            "ends_on": extractor.deprecated_until.isoformat(),
        }
        successor = _successor_for(
            extractor.deprecated_successor, str(self.entry.data.get(CONF_REGION, ""))
        )
        if successor is not None:
            placeholders["successor"] = successor.label
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            # Only tell the user to switch to the successor when we can
            # actually price it for their region. Naming a supplier the
            # config flow will refuse (it aborts at the contract step with
            # supplier_region_unavailable) sends them down a dead end;
            # the fallback card states the situation without the bad advice.
            #
            # Past the end date the same card would read "is transferring your
            # contract on <a date in the past>" and "nothing is broken yet",
            # both false by then, so the elapsed variants say the transfer has
            # happened and this entry has stopped updating.
            translation_key=(
                ("supplier_deprecated_ended" if ended else "supplier_deprecated")
                if successor is not None
                else (
                    "supplier_deprecated_ended_no_successor"
                    if ended
                    else "supplier_deprecated_no_successor"
                )
            ),
            # Labels, not registry ids: the card tells the user to pick a
            # supplier from a label-based dropdown, so "DATS 24" and
            # "EnergyVision" are what they will actually look for.
            translation_placeholders=placeholders,
        )

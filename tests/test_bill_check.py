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

"""The lines of an Eneco bill held to what the entry bills for the same days.

The bill below has the lines, periods and unit prices of a yearly Walloon
bill on the October 2022 Flex card (Eneco's public tariffs, ORES, mono meter,
compensation). Its quantities are made up.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import data_entry_flow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockEntityPlatform

from custom_components.be_electricity_prices import bill_check, snapshot_months
from custom_components.be_electricity_prices.bill_check import (
    BillCheckSensor,
    check_bill,
)
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
from custom_components.be_electricity_prices.sensor import async_setup_entry
from tests import fixture_text, make_entry, make_snapshot
from tests.test_eneco import (
    _archived_flex_months,
    _index_document,
    _quarterly_card,
    _run,
)

_DATA: dict[str, Any] = {
    "contract": "power_flex",
    "tariff_card_date": "2022-10-01",
    "solar_regime": "compensation",
    "solar_kva": 5.0,
    "card_archive": True,
}

_YEAR = ("2025-02-01", "2026-01-31")


def _line(
    section: str,
    label: str,
    code: str | None,
    period: tuple[str, str],
    unit: str | None,
    price: float | None,
    vat: float = 0.06,
) -> dict[str, Any]:
    """A line as the eneco_be sensor publishes it, on a made-up quantity."""
    quantity = None if unit is None else (1.0 if unit == "year" else 300.0)
    total = round(quantity * price, 2) if quantity and price else -12.34
    return {
        "section": section,
        "label": label,
        "code": code,
        "start": period[0],
        "end": period[1],
        "quantity": quantity,
        "unit": unit,
        "unit_price": price,
        "vat": vat,
        "total": total,
    }


_ENERGY = "Energiecomponent ElectricityAplusTH"
_BILL = [
    _line("energy", "Redevance fixe", "Vaste vergoeding", _YEAR, "year", 49.59),
    _line(
        "energy", "Conso. simple", _ENERGY, ("2025-02-01", "2025-03-31"), "kWh", 0.1735
    ),
    _line(
        "energy", "Conso. simple", _ENERGY, ("2025-04-01", "2025-06-30"), "kWh", 0.1291
    ),
    _line(
        "energy", "Conso. simple", _ENERGY, ("2025-07-01", "2025-09-30"), "kWh", 0.1342
    ),
    _line(
        "energy", "Conso. simple", _ENERGY, ("2025-10-01", "2025-12-31"), "kWh", 0.1450
    ),
    _line(
        "energy", "Conso. simple", _ENERGY, ("2026-01-01", "2026-01-31"), "kWh", 0.1589
    ),
    _line(
        "energy", "Coûts énergie verte", "GSC administratiekost", _YEAR, "kWh", 0.0283
    ),
    _line(
        "network",
        "Distribution variable (kWh)",
        "Distributie variabel (per kWh)",
        ("2025-02-01", "2025-12-31"),
        "kWh",
        0.1018,
    ),
    _line(
        "network",
        "Distribution variable (kWh)",
        "Distributie variabel (per kWh)",
        ("2026-01-01", "2026-01-31"),
        "kWh",
        0.1130,
    ),
    _line(
        "network",
        "Distribution fixe",
        "Distributie vast (per jaar)",
        ("2025-02-01", "2025-02-28"),
        "year",
        13.04,
    ),
    _line(
        "network",
        "Distribution fixe",
        "Distributie vast (per jaar)",
        ("2025-03-01", "2025-12-31"),
        "year",
        13.06,
    ),
    _line(
        "network",
        "Distribution fixe",
        "Distributie vast (per jaar)",
        ("2026-01-01", "2026-01-31"),
        "year",
        13.30,
    ),
    _line(
        "network",
        "Tarif prosumer plafonnement",
        "Prosumententarief plafonnering",
        _YEAR,
        None,
        None,
    ),
    _line(
        "network",
        "Coûts de transmission (kWh)",
        "Transport variabel (per kWh)",
        ("2025-02-01", "2025-02-28"),
        "kWh",
        0.0302,
    ),
    _line(
        "network",
        "Coûts de transmission (kWh)",
        "Transport variabel (per kWh)",
        ("2025-03-01", "2025-12-31"),
        "kWh",
        0.0281,
    ),
    _line(
        "network",
        "Coûts de transmission (kWh)",
        "Transport variabel (per kWh)",
        ("2026-01-01", "2026-01-31"),
        "kWh",
        0.0259,
    ),
    _line("taxes", "Redevance de raccordement", None, _YEAR, "kWh", 0.0008, vat=0.0),
    _line("taxes", "Accise spéciale", None, _YEAR, "kWh", 0.0475),
    _line("taxes", "Cotisation sur l'énergie", None, _YEAR, "kWh", 0.0019),
]


def _checked(lines: list[Any], **data: Any) -> list[dict[str, Any]] | None:
    """``lines`` checked by an entry on the October 2022 Flex card, each month
    on its archived row and the signing card, as the month cache has them."""
    signing = _quarterly_card(
        {"012210": fixture_text("eneco_flex_oct22.pdf")}, date(2022, 10, 1)
    )
    archived = _archived_flex_months()

    async def row(*args: Any, **_k: Any) -> Any:
        month = args[5]
        return signing if month == date(2022, 10, 1) else archived.get(month)

    entry = make_entry(**(_DATA | data))
    coordinator = SimpleNamespace(
        hass=SimpleNamespace(data={}),
        entry=entry,
        _supplier_tuple=("eneco", "power_flex", "wallonia"),
        _snapshot=archived[date(2026, 11, 1)],
        _snapshot_raw=archived[date(2026, 11, 1)],
        _session=None,
    )
    with (
        patch.object(snapshot_months, "_month_row", new=row),
        patch(
            "custom_components.be_electricity_prices.providers._eneco_index.fetch_pdf_text",
            new=_index_document(),
        ),
    ):
        return _run(check_bill(coordinator, {"lines": lines}))  # type: ignore[arg-type]


def _outcome(results: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [(r["label"], r["start"], r["status"]) for r in results]


@pytest.mark.parametrize("keyed", [False, True], ids=["keyless", "keyed"])
def test_the_yearly_bill_on_the_october_2022_card(freezer: Any, keyed: bool) -> None:
    """Every line the entry prices comes out at the bill's figure, but the
    green cost, which the month cards print at 2,90 to 2,94 c€ before VAT
    against the 2,83 billed, and February 2025's fixed distribution term,
    which the February card prints at March's 13,06 where ORES billed 13,04.
    The prosumer cap is a flat amount and is left out."""
    freezer.move_to("2026-11-24 12:00:00+01:00")
    results = _checked(_BILL, **({"api_key": "k"} if keyed else {}))
    assert results is not None
    assert _outcome(results) == [
        ("Redevance fixe", "2025-02-01", "ok"),
        ("Conso. simple", "2025-02-01", "ok"),
        ("Conso. simple", "2025-04-01", "ok"),
        ("Conso. simple", "2025-07-01", "ok"),
        ("Conso. simple", "2025-10-01", "ok"),
        ("Conso. simple", "2026-01-01", "ok"),
        ("Coûts énergie verte", "2025-02-01", "off"),
        ("Distribution variable (kWh)", "2025-02-01", "ok"),
        ("Distribution variable (kWh)", "2026-01-01", "ok"),
        ("Distribution fixe", "2025-02-01", "off"),
        ("Distribution fixe", "2025-03-01", "ok"),
        ("Distribution fixe", "2026-01-01", "ok"),
        ("Tarif prosumer plafonnement", "2025-02-01", "not_compared"),
        ("Coûts de transmission (kWh)", "2025-02-01", "ok"),
        ("Coûts de transmission (kWh)", "2025-03-01", "ok"),
        ("Coûts de transmission (kWh)", "2026-01-01", "ok"),
        ("Redevance de raccordement", "2025-02-01", "ok"),
        ("Accise spéciale", "2025-02-01", "ok"),
        ("Cotisation sur l'énergie", "2025-02-01", "ok"),
    ]
    by_label = {(r["label"], r["start"]): r for r in results}
    green = by_label[("Coûts énergie verte", "2025-02-01")]
    # Every month's own figure, none of them a blend.
    assert green["priced"] == pytest.approx([0.028962, 0.029434])
    assert by_label[("Redevance fixe", "2025-02-01")]["priced"] == pytest.approx(
        52.56 / 1.06, abs=1e-6
    )
    # Exempt from VAT: as the card prints it, and within the bill's rounding.
    assert by_label[("Redevance de raccordement", "2025-02-01")]["priced"] == (
        pytest.approx(0.00075)
    )
    assert by_label[("Distribution fixe", "2025-02-01")]["priced"] == pytest.approx(
        13.84 / 1.06, abs=1e-6
    )
    cap = by_label[("Tarif prosumer plafonnement", "2025-02-01")]
    assert cap["reason"] == "a flat amount set by the period's volumes"


def _one(label: str, code: str | None, period: tuple[str, str], **kw: Any) -> Any:
    unit = kw.pop("unit", "kWh")
    price = kw.pop("price", 0.1735)
    results = _checked([_line("energy", label, code, period, unit, price)], **kw)
    assert results is not None
    return results[0]


def test_a_line_across_two_rates_is_held_to_each(freezer: Any) -> None:
    """March and April 2025 bill 17,35 and 12,91: a line across both at
    either figure is not a mismatch the entry can prove, one priced above both
    is, and neither is averaged."""
    freezer.move_to("2026-11-24 12:00:00+01:00")
    span = ("2025-03-01", "2025-04-30")
    at_march = _one("Conso. simple", _ENERGY, span, price=0.1735)
    assert at_march["status"] == "not_compared"
    assert at_march["reason"] == "the entry's rate changes within these dates"
    assert at_march["priced"] == pytest.approx([0.12907, 0.17351])
    above = _one("Conso. simple", _ENERGY, span, price=0.19)
    assert above["status"] == "off"
    below = _one("Conso. simple", _ENERGY, span, price=0.12)
    assert below["status"] == "off"


def test_a_line_is_matched_by_its_type_then_by_its_label(freezer: Any) -> None:
    """The hidden line type decides; the printed label stands in where the
    bill carries none, typed with either apostrophe; anything else is left
    out rather than guessed."""
    freezer.move_to("2026-11-24 12:00:00+01:00")
    march = ("2025-03-01", "2025-03-31")
    assert _one("Conso. simple", None, march)["status"] == "ok"
    assert _one("Enkelvoudig", _ENERGY, march)["status"] == "ok"
    assert (
        _one("Cotisation sur l\u2019énergie", None, march, price=0.0019)["status"]
        == "ok"
    )
    unknown = _one("Tarif prosumer", None, march)
    assert unknown["status"] == "not_compared"
    assert unknown["reason"] == "no matching charge"
    assert _one("Conso. heures pleines", None, march)["reason"] == (
        "the entry bills a single register"
    )
    assert _one("Conso. simple", _ENERGY, march, meter="bi")["reason"] == (
        "the entry bills a day and a night register"
    )
    assert _one("Conso. simple", _ENERGY, march, unit="kVA")["reason"] == (
        "billed per kVA, priced per kWh"
    )
    assert (
        _one("Distribution variable (kWh)", None, march, region="flanders")["reason"]
        == "network lines are compared on Walloon bills only"
    )


def test_a_month_without_a_card_of_its_own_is_left_out(freezer: Any) -> None:
    """December 2022 has no archived card: the current card would stand in
    for it in the cost walks, which is not what that month was billed."""
    freezer.move_to("2026-11-24 12:00:00+01:00")
    old = _one("Conso. simple", _ENERGY, ("2022-12-01", "2022-12-31"))
    assert old["status"] == "not_compared"
    assert old["reason"] == "no tariff card for 2022-12"


def test_a_line_that_does_not_read_is_left_out(freezer: Any) -> None:
    freezer.move_to("2026-11-24 12:00:00+01:00")
    broken = _line(
        "energy", "Conso. simple", _ENERGY, ("2025-03-31", "2025-03-01"), "kWh", 0.17
    )
    results = _checked([broken, {"label": 3}, "x"])
    assert results is not None
    assert [r["status"] for r in results] == ["not_compared"] * 3
    assert results[0]["reason"] == "the line does not read"


def test_nothing_is_checked_without_lines_or_a_card() -> None:
    coordinator = SimpleNamespace(_snapshot=None)
    assert _run(check_bill(coordinator, {"lines": _BILL})) is None  # type: ignore[arg-type]
    coordinator = SimpleNamespace(_snapshot=object())
    assert _run(check_bill(coordinator, {})) is None  # type: ignore[arg-type]
    assert _run(check_bill(coordinator, {"lines": []})) is None  # type: ignore[arg-type]


_RESULTS = [
    {
        "label": "Conso. simple",
        "start": "2025-02-01",
        "end": "2025-03-31",
        "status": "ok",
    },
    {
        "label": "Coûts énergie verte",
        "start": "2025-02-01",
        "end": "2026-01-31",
        "status": "off",
    },
]
_BILL_ENTITY = "sensor.meter_last_bill"
_ATTRIBUTES = {
    "issued": "2026-02-12",
    "period_start": "2025-02-01",
    "period_end": "2026-01-31",
    "lines": _BILL,
}


async def test_the_sensor_counts_the_lines_off(hass: HomeAssistant) -> None:
    """The state counts the lines off and the lines ride unrecorded; a bill
    sensor with nothing read leaves it unknown."""
    entry = make_entry(**_DATA, eneco_bill=_BILL_ENTITY)
    entry.add_to_hass(hass)
    coordinator = BePricesCoordinator(hass, entry)
    coordinator._set_snapshot(make_snapshot())
    hass.states.async_set(_BILL_ENTITY, "1074.87", _ATTRIBUTES)
    checked = AsyncMock(return_value=_RESULTS)
    sensor = BillCheckSensor(coordinator, _BILL_ENTITY)
    with patch.object(bill_check, "check_bill", new=checked):
        await MockEntityPlatform(hass).async_add_entities([sensor])
        await hass.async_block_till_done(wait_background_tasks=True)
        state = hass.states.get(sensor.entity_id)
        assert state is not None
        assert state.state == "1"
        assert state.attributes["issued"] == "2026-02-12"
        assert state.attributes["period_end"] == "2026-01-31"
        assert state.attributes["lines"] == _RESULTS
        assert "lines" in sensor._unrecorded_attributes

        checked.return_value = [_RESULTS[0]]
        hass.states.async_set(_BILL_ENTITY, "1074.88", _ATTRIBUTES)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get(sensor.entity_id).state == "0"  # type: ignore[union-attr]

        # A bill sensor with nothing read: unknown.
        checked.return_value = None
        hass.states.async_set(_BILL_ENTITY, "unknown", {})
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get(sensor.entity_id).state == "unknown"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("picked", "count"), [({}, 0), ({"eneco_bill": _BILL_ENTITY}, 1)]
)
async def test_the_sensor_exists_only_with_a_bill_picked(
    picked: dict[str, str], count: int
) -> None:
    from custom_components.be_electricity_prices.coordinator_data import (
        CoordinatorData,
    )

    entry = make_entry(**(_DATA | picked))
    entry.runtime_data = SimpleNamespace(
        data=CoordinatorData(), entry=entry, intended_unique_ids={}
    )
    added: list[Any] = []
    await async_setup_entry(
        None,  # type: ignore[arg-type]
        entry,
        lambda entities: added.extend(entities),  # type: ignore[arg-type]
    )
    found = [e for e in added if isinstance(e, BillCheckSensor)]
    assert len(found) == count
    if found:
        assert found[0].unique_id == f"{entry.entry_id}_bill_check"
        assert found[0].unique_id in entry.runtime_data.intended_unique_ids["sensor"]


async def _to_meters(
    hass: HomeAssistant, entry: Any, supplier: str, contract: str = "power_flex"
) -> Any:
    """The edit branch walked to the meters step on the entry's own answers."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit"}
    )
    answers: dict[str, dict[str, Any]] = {
        "edit": {"supplier": supplier, "region": "wallonia"},
        "contract": {"contract": contract},
        "dso": {"dso": "ores"},
        "meter": {"meter": "mono"},
        "solar": {"solar_kva": "5.0", "solar_regime": "compensation"},
    }
    while result["step_id"] != "meters":
        assert result["type"] == data_entry_flow.FlowResultType.FORM, result
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], answers.get(result["step_id"], {})
        )
    return result


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_meters_step_takes_a_last_bill_sensor_only(
    hass: HomeAssistant,
) -> None:
    """Offered on an Eneco entry; another sensor of the eneco integration
    is refused, a last bill sensor is kept, and a blank picker clears it."""
    registry = er.async_get(hass)
    bill = registry.async_get_or_create("sensor", "eneco_be", "meter_last_bill")
    status = registry.async_get_or_create("sensor", "eneco_be", "meter_status")
    entry = make_entry(**_DATA)
    entry.add_to_hass(hass)

    result = await _to_meters(hass, entry, "eneco")
    assert "eneco_bill" in result["data_schema"].schema
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"eneco_bill": status.entity_id}
    )
    assert result["errors"] == {"eneco_bill": "eneco_bill_not_a_bill"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"eneco_bill": bill.entity_id}
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.data["eneco_bill"] == bill.entity_id

    result = await _to_meters(hass, entry, "eneco")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert "eneco_bill" not in entry.data


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_another_supplier_is_offered_no_bill_and_drops_one(
    hass: HomeAssistant,
) -> None:
    entry = make_entry(**(_DATA | {"eneco_bill": _BILL_ENTITY}))
    entry.add_to_hass(hass)
    result = await _to_meters(hass, entry, "cociter", "cociter_variable")
    assert "eneco_bill" not in result["data_schema"].schema
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert "eneco_bill" not in entry.data


def test_the_bill_sensor_stays_out_of_the_diagnostics() -> None:
    """Its entity id is named after the meter's EAN."""
    from custom_components.be_electricity_prices.diagnostics import TO_REDACT

    assert "eneco_bill" in TO_REDACT

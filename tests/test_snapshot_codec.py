"""The snapshot codec against rows other versions wrote or will write."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.be_electricity_prices.providers._rates import (
    DynamicRates,
    EnergyRates,
    FixedRates,
    ImpactRates,
    InjectionRates,
    SpotMonthlyRates,
    TimeOfUseRates,
    VariableRates,
)
from custom_components.be_electricity_prices.snapshot_codec import (
    _snapshot_from_dict,
    _snapshot_to_dict,
)
from tests import make_snapshot

NOW = datetime(2026, 9, 12, 6, 0, tzinfo=UTC)


def test_a_register_pair_flag_is_written_only_when_set() -> None:
    """The card archive is read by every installed version, and the ones
    from before the flag cannot decode a row that carries it: a row of any
    other supplier keeps the shape it had, and only a card that sets the
    flag carries it, where it round-trips."""
    plain = make_snapshot(injection=InjectionRates(current=0.05))
    row = _snapshot_to_dict(plain, NOW)
    assert "bi_hourly" not in row["injection"]
    assert _snapshot_from_dict(row).injection == plain.injection
    pair = make_snapshot(
        injection=InjectionRates(current=0.05, peak=0.06, offpeak=0.04, bi_hourly=True)
    )
    row = _snapshot_to_dict(pair, NOW)
    assert row["injection"]["bi_hourly"] is True
    assert _snapshot_from_dict(row).injection == pair.injection


def test_a_field_this_version_does_not_know_is_dropped_not_refused() -> None:
    row = _snapshot_to_dict(make_snapshot(injection=InjectionRates(current=0.05)), NOW)
    row["injection"]["from_the_future"] = True
    assert _snapshot_from_dict(row).injection == InjectionRates(current=0.05)


@pytest.mark.parametrize(
    "energy",
    [
        FixedRates(single=0.20),
        VariableRates(current=0.21),
        DynamicRates(factor=1.0, base=0.02),
        TimeOfUseRates(peak=0.25, offpeak=0.18, transition=0.21),
        ImpactRates(eco=0.15, medium=0.20, pic=0.28),
        SpotMonthlyRates(factor=1.0, base=0.01),
    ],
    ids=["fixed", "variable", "dynamic", "tou", "impact", "spot_monthly"],
)
def test_every_energy_kind_survives_a_field_from_a_later_version(
    energy: EnergyRates,
) -> None:
    """Each of the six energy kinds is rebuilt by its own `_known_fields` call,
    and the test beside this one exercises exactly one of them, because
    `make_snapshot`'s default leg is `FixedRates`.

    Deleting any of the other five calls left that test green: 4 passed with
    the variable, dynamic, TOU, Impact or spot-monthly guard removed. A field
    added to one of those rate classes later would then take the whole row
    down on every older reader, which is the failure this rule exists to stop.
    """
    row = _snapshot_to_dict(make_snapshot(energy=energy), NOW)
    row["energy"]["from_the_future"] = 1
    assert _snapshot_from_dict(row).energy == energy


def test_every_leg_of_a_row_survives_a_field_from_a_later_version() -> None:
    """Every archived row is read by every installed version, so a field added
    later reaches an older reader as an unexpected keyword: TypeError, the card
    dropped, the entry back on the supplier tier, and a debug line as the only
    trace. The failure lands on the users who did not upgrade.

    The injection leg had that rule and it is one of the eight dataclasses a
    row is rebuilt from. Nothing is exposed today; the next field added to an
    overlay or a rate class is.
    """
    row = _snapshot_to_dict(make_snapshot(injection=InjectionRates(current=0.05)), NOW)
    row["energy"]["from_the_future"] = 1
    row["taxes"]["from_the_future"] = 2
    for overlay in row["dsos"].values():
        overlay["from_the_future"] = 3
    row["injection"]["from_the_future"] = 4

    rebuilt = _snapshot_from_dict(row)
    original = make_snapshot(injection=InjectionRates(current=0.05))
    assert rebuilt.energy == original.energy
    assert rebuilt.taxes == original.taxes
    assert rebuilt.dsos == original.dsos
    assert rebuilt.injection == original.injection


def test_a_settled_index_is_written_only_when_the_month_has_one() -> None:
    """Same rule as the register pair, and for the same reason: the card
    archive holds 1.686 rows and 1.683 of them carry an injection leg, so a
    field written at its default on every one of them is 1.683 rewritten rows
    that are not changed cards.

    The test for "no value here" is the dataclass DEFAULT, not falsiness. A
    month whose index settled at exactly 0 EUR/MWh is a settled month, and
    dropping its field would send the engine back to computing a mean for a
    month the supplier has already published.
    """
    plain = make_snapshot(injection=InjectionRates(current=0.05))
    row = _snapshot_to_dict(plain, NOW)
    assert "index_realised" not in row["injection"]
    assert _snapshot_from_dict(row).injection == plain.injection

    for index in (0.07911, 0.0):
        settled = make_snapshot(
            injection=InjectionRates(current=0.05, index_realised=index)
        )
        row = _snapshot_to_dict(settled, NOW)
        assert row["injection"]["index_realised"] == index
        assert _snapshot_from_dict(row).injection == settled.injection

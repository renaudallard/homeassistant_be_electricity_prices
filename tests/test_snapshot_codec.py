"""The snapshot codec against rows other versions wrote or will write."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.be_electricity_prices.providers.base import InjectionRates
from custom_components.be_electricity_prices.snapshot_store import (
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

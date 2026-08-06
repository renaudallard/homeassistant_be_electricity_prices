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

"""Energy-dashboard pre-fill for the config flow's meter and capacity steps.

Split out of ``config_flow.py``. Everything here reads Home Assistant's own
Energy dashboard configuration and the entity registry to suggest defaults the
user would otherwise type by hand; nothing here is load-bearing, so every
failure mode collapses to "suggest nothing" rather than breaking the wizard.

The Energy-manager and entity-registry imports stay FUNCTION-LOCAL: the tests
patch them at their absolute path, and ``homeassistant.components.energy`` is
not guaranteed to be installed.
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    CONF_CAPACITY_PEAK_SENSOR,
    CONF_CONSUMPTION_KWH,
    CONF_DAY_CONSUMPTION_KWH,
    CONF_DAY_INJECTION_KWH,
    CONF_INJECTION_KWH,
    CONF_NIGHT_CONSUMPTION_KWH,
    CONF_NIGHT_INJECTION_KWH,
)

_DAY_TARIFF_TOKENS = frozenset({"peak", "day", "jour", "dag", "piek"})
_NIGHT_TARIFF_TOKENS = frozenset({"night", "nuit", "nacht", "dal"})
_TARIFF_SEPARATORS = re.compile(r"[_\-\s]+")


def _classify_tariff(name: str) -> str | None:
    """Map a utility_meter tariff name to ``"day"`` / ``"night"``.

    Belgian users mix English (peak/offpeak), French (jour/nuit), and
    Dutch (dag/nacht, piek/dal) when naming their utility_meter
    tariffs. Tokenize on ``_-`` and whitespace and match exactly so
    "offpeak" doesn't accidentally collide with "peak". Names with
    both a day and a night token (e.g. "peak_night_combined") return
    ``None`` so the caller can refuse to pre-fill rather than guess.
    """
    n = name.lower()
    # "offpeak" / "off_peak" / "off-peak" all collapse to a contiguous
    # "offpeak"; treat that as night regardless of token splitting.
    if "offpeak" in _TARIFF_SEPARATORS.sub("", n):
        return "night"
    tokens = set(_TARIFF_SEPARATORS.split(n))
    is_day = bool(tokens & _DAY_TARIFF_TOKENS)
    is_night = bool(tokens & _NIGHT_TARIFF_TOKENS)
    if is_day and not is_night:
        return "day"
    if is_night and not is_day:
        return "night"
    return None


def _utility_meter_day_night_children(
    hass: HomeAssistant, source_entity_id: str
) -> dict[str, str]:
    """Return ``{"day": ..., "night": ...}`` entity ids for a
    utility_meter helper splitting ``source_entity_id`` into a day /
    night pair, or ``{}`` if no unambiguous match is found.

    Walks two paths:

    1. ``utility_meter`` config entries (modern UI-configured helpers).
       These store ``source`` + ``tariffs`` in entry options and their
       per-tariff child sensors share the entry's config_entry_id.

    2. Entity-registry entries with ``platform == "utility_meter"`` and
       no config_entry_id (YAML-configured helpers; common in older
       HA installs). The source + tariff name come from the live
       state attributes set by the utility_meter component.

    Bails on any ambiguity rather than guessing -- a wrong day/night
    pick mis-bills the year cost.
    """
    from homeassistant.helpers import entity_registry as er

    for entry in hass.config_entries.async_entries("utility_meter"):
        opts = {**entry.data, **entry.options}
        if opts.get("source") != source_entity_id:
            continue
        tariffs = opts.get("tariffs") or []
        slot_tariffs: dict[str, str] = {}
        ambiguous = False
        for tariff in tariffs:
            slot = _classify_tariff(tariff)
            if slot is None:
                continue
            if slot in slot_tariffs:
                ambiguous = True
                break
            slot_tariffs[slot] = tariff
        if ambiguous or "day" not in slot_tariffs or "night" not in slot_tariffs:
            continue
        ent_reg = er.async_get(hass)
        registry_entries = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        out: dict[str, str] = {}
        for slot, tariff in slot_tariffs.items():
            # Match the child id exactly. utility_meter builds them as
            # f"{entry_id}_{tariff}", so a suffix test binds the wrong child
            # whenever one tariff name ends with another across an
            # underscore: with the common ["off_peak", "peak"] pair,
            # "<entry>_off_peak" also ends with "_peak", and the day slot
            # would take the off-peak register, billing night kWh at the
            # day rate.
            expected = f"{entry.entry_id}_{tariff}"
            for re_entry in registry_entries:
                if re_entry.unique_id == expected:
                    out[slot] = re_entry.entity_id
                    break
        # Two slots resolving to one entity means the match was wrong, not
        # that the helper has one register; pre-filling both with it would
        # bill the same kWh twice at two different rates.
        if "day" in out and "night" in out and out["day"] != out["night"]:
            return out

    # YAML-rooted helpers: walk the entity registry for utility_meter
    # children whose runtime ``source`` attribute matches our grid
    # sensor. The ``tariff`` attribute carries the configured tariff
    # name, which we classify the same way as UI-configured tariffs.
    ent_reg = er.async_get(hass)
    yaml_slot_to_entity: dict[str, str] = {}
    for re_entry in ent_reg.entities.values():
        if re_entry.platform != "utility_meter":
            continue
        if re_entry.config_entry_id is not None:
            continue  # UI-configured, already handled above
        state = hass.states.get(re_entry.entity_id)
        if state is None:
            continue
        if state.attributes.get("source") != source_entity_id:
            continue
        tariff_name = str(state.attributes.get("tariff") or "")
        slot = _classify_tariff(tariff_name)
        if slot is None:
            continue
        if slot in yaml_slot_to_entity:
            return {}  # ambiguous: two YAML children for the same slot
        yaml_slot_to_entity[slot] = re_entry.entity_id
    if "day" in yaml_slot_to_entity and "night" in yaml_slot_to_entity:
        return yaml_slot_to_entity
    return {}


async def _energy_grid_source(hass: HomeAssistant) -> dict[str, Any] | None:
    """The Energy dashboard's FIRST grid source, or None.

    Both pre-fill helpers open-coded this: the guarded import, the manager
    load, the empty-prefs check and the walk to the first ``type == "grid"``
    entry. The import has to stay inside the function (the tests patch the
    absolute path, and ``energy`` may not be installed), and every failure mode
    collapses to None because a pre-fill must never break the wizard.
    """
    try:
        from homeassistant.components.energy.data import async_get_manager
    except ImportError:
        return None
    try:
        manager = await async_get_manager(hass)
    except Exception:  # noqa: BLE001 - energy may not be ready
        return None
    prefs: dict[str, Any] | None = manager.data  # type: ignore[assignment]
    if not prefs:
        return None
    for source in prefs.get("energy_sources") or []:
        if source.get("type") == "grid":
            return source  # type: ignore[no-any-return]
    return None


async def _apply_energy_manager_defaults(
    hass: HomeAssistant, defaults: dict[str, Any]
) -> None:
    """Pre-fill the cumulative consumption / injection sensors (and,
    when a utility_meter helper is wired up, the day/night registers)
    from the user's Energy dashboard when nothing is already set.

    The Energy dashboard's grid source records the same kind of
    cumulative-kWh totals the coordinator reads via the recorder, so
    treating it as the default saves the user from picking the same
    sensor twice. For the day/night split we follow utility_meter
    helpers rooted at the same source -- only when the tariff names
    map unambiguously to day/night.
    """
    if any(
        defaults.get(k) is not None
        for k in (
            CONF_CONSUMPTION_KWH,
            CONF_INJECTION_KWH,
            CONF_DAY_CONSUMPTION_KWH,
            CONF_NIGHT_CONSUMPTION_KWH,
            CONF_DAY_INJECTION_KWH,
            CONF_NIGHT_INJECTION_KWH,
        )
    ):
        return
    source = await _energy_grid_source(hass)
    if source is not None:
        flow_from: list[dict[str, Any]] = source.get("flow_from") or []
        flow_to: list[dict[str, Any]] = source.get("flow_to") or []
        consumption_stat: str | None = None
        injection_stat: str | None = None
        if flow_from:
            stat = flow_from[0].get("stat_energy_from")
            # EntitySelector only accepts real entities; recorder-only
            # statistic ids (no leading "sensor.") would render as a
            # broken default.
            if isinstance(stat, str) and stat.startswith("sensor."):
                consumption_stat = stat
        if flow_to:
            stat = flow_to[0].get("stat_energy_to")
            if isinstance(stat, str) and stat.startswith("sensor."):
                injection_stat = stat
        if consumption_stat is not None:
            defaults[CONF_CONSUMPTION_KWH] = consumption_stat
            day_night = _utility_meter_day_night_children(hass, consumption_stat)
            if day_night:
                defaults[CONF_DAY_CONSUMPTION_KWH] = day_night["day"]
                defaults[CONF_NIGHT_CONSUMPTION_KWH] = day_night["night"]
        if injection_stat is not None:
            defaults[CONF_INJECTION_KWH] = injection_stat
            day_night = _utility_meter_day_night_children(hass, injection_stat)
            if day_night:
                defaults[CONF_DAY_INJECTION_KWH] = day_night["day"]
                defaults[CONF_NIGHT_INJECTION_KWH] = day_night["night"]
        return


def _dsmr_monthly_peak_sensor(hass: HomeAssistant) -> str | None:
    """The Belgian meter's own monthly peak entity, when the user has one.

    Fluvius bills the highest quarter-hour offtake of the month, and a DSMR
    5B meter publishes exactly that on the P1 port; Home Assistant's built-in
    ``dsmr`` integration surfaces it as ``maximum_demand_current_month``. That
    entity is strictly better than anything derived from an instantaneous
    power sensor: the meter computes the true quarter-hour average, so no
    sampling of ours can miss a peak between reads or mistake a momentary
    spike for a quarter-hour one.

    Matched on the registry's ``translation_key`` rather than the entity id,
    which the user is free to rename.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for entry in registry.entities.values():
        if (
            entry.domain == "sensor"
            and entry.platform == "dsmr"
            and entry.translation_key == "maximum_demand_current_month"
            and not entry.disabled
        ):
            return entry.entity_id
    return None


async def _apply_energy_manager_capacity_default(
    hass: HomeAssistant, defaults: dict[str, Any]
) -> None:
    """Pre-fill the Flemish capacity peak sensor when nothing is already set.

    Prefer the meter's own monthly peak (see ``_dsmr_monthly_peak_sensor``).
    Only when there is none do we fall back to the Energy dashboard walk
    below, which yields an instantaneous power sensor: the coordinator then
    samples it once an hour and keeps a rolling max, which is a rough
    estimate of a quarter-hour peak rather than the billed quantity.

    The dashboard tracks cumulative kWh, but the capacity tariff needs
    a kW power sensor. The common bridge is a Riemann ``integration``
    helper that turns a kW input into the kWh output the dashboard
    consumes. Walk back: dashboard kWh sensor -> integration helper
    config entry -> the helper's ``source`` (the kW sensor we want).

    Skipped when:
      - the user already picked a sensor (preserve manual choice),
      - the energy component isn't loaded,
      - the dashboard has no grid source,
      - the consumption sensor isn't a Riemann-integration child
        (no way to derive the kW source automatically).
    """
    if defaults.get(CONF_CAPACITY_PEAK_SENSOR) is not None:
        return
    if (meter_peak := _dsmr_monthly_peak_sensor(hass)) is not None:
        defaults[CONF_CAPACITY_PEAK_SENSOR] = meter_peak
        return
    consumption_stat: str | None = None
    source = await _energy_grid_source(hass)
    if source is not None:
        flow_from: list[dict[str, Any]] = source.get("flow_from") or []
        if flow_from:
            stat = flow_from[0].get("stat_energy_from")
            if isinstance(stat, str) and stat.startswith("sensor."):
                consumption_stat = stat
    if consumption_stat is None:
        return
    from homeassistant.helpers import entity_registry as er

    ent_reg = er.async_get(hass)
    re_entry = ent_reg.async_get(consumption_stat)
    if re_entry is None or re_entry.platform != "integration":
        return
    if re_entry.config_entry_id is None:
        return
    ce = hass.config_entries.async_get_entry(re_entry.config_entry_id)
    if ce is None:
        return
    opts = {**ce.data, **ce.options}
    source_sensor = opts.get("source")
    if not isinstance(source_sensor, str) or not source_sensor.startswith("sensor."):
        return
    # Validate the candidate is an actual power sensor before pre-filling.
    # A Riemann source can in principle be anything numeric (a flow rate,
    # a temperature delta...); pre-filling a non-power sensor used to put
    # the user one click away from issue #19. When unsure, leave the
    # field blank so the (now device_class-filtered) picker forces a
    # deliberate choice.
    state = hass.states.get(source_sensor)
    if state is None:
        return
    device_class = state.attributes.get("device_class")
    unit = (state.attributes.get("unit_of_measurement") or "").strip()
    if device_class not in ("power", "apparent_power") and unit not in (
        "W",
        "kW",
        "VA",
        "kVA",
    ):
        return
    defaults[CONF_CAPACITY_PEAK_SENSOR] = source_sensor

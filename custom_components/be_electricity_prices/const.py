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

"""Constants for the Belgian Electricity Prices integration.

No prices live here - all rates come from per-provider live extractors.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "be_electricity_prices"

PLATFORMS: Final = ("sensor", "binary_sensor")

REGION_FLANDERS: Final = "flanders"
REGION_WALLONIA: Final = "wallonia"
REGION_BRUSSELS: Final = "brussels"

REGIONS: Final = (REGION_FLANDERS, REGION_WALLONIA, REGION_BRUSSELS)

# Canonical DSO sub-area keys. Stable forever: stored verbatim in
# every user's CONF_DSO and surfaced as keys in SupplierSnapshot.dsos,
# so renaming would silently break every existing entry. Per-provider
# extractors map their PDF labels onto these.
DSO_FLUVIUS_ANTWERPEN: Final = "fluvius_antwerpen"
DSO_FLUVIUS_HALLE_VILVOORDE: Final = "fluvius_halle_vilvoorde"
DSO_FLUVIUS_IMEWO: Final = "fluvius_imewo"
DSO_FLUVIUS_INTERGEM: Final = "fluvius_intergem"
DSO_FLUVIUS_IVEKA: Final = "fluvius_iveka"
DSO_FLUVIUS_LIMBURG: Final = "fluvius_limburg"
DSO_FLUVIUS_WEST: Final = "fluvius_west"
DSO_FLUVIUS_ZENNE_DIJLE: Final = "fluvius_zenne_dijle"

DSO_AIEG: Final = "aieg"
DSO_AIESH: Final = "aiesh"
DSO_ORES: Final = "ores"
DSO_RESA: Final = "resa"
DSO_REW: Final = "rew"

DSO_SIBELGA: Final = "sibelga"

FLUVIUS_KEYS: Final[frozenset[str]] = frozenset(
    {
        DSO_FLUVIUS_ANTWERPEN,
        DSO_FLUVIUS_HALLE_VILVOORDE,
        DSO_FLUVIUS_IMEWO,
        DSO_FLUVIUS_INTERGEM,
        DSO_FLUVIUS_IVEKA,
        DSO_FLUVIUS_LIMBURG,
        DSO_FLUVIUS_WEST,
        DSO_FLUVIUS_ZENNE_DIJLE,
    }
)
WALLONIA_DSO_KEYS: Final[frozenset[str]] = frozenset(
    {DSO_AIEG, DSO_AIESH, DSO_ORES, DSO_RESA, DSO_REW}
)

# DSO selection per region. Flanders has eight Fluvius sub-areas with
# materially different distribution rates; Wallonia DSOs are uniform per
# operator; Brussels has one (Sibelga).
DSO_CHOICES: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    REGION_FLANDERS: (
        (DSO_FLUVIUS_ANTWERPEN, "Fluvius Antwerpen"),
        (DSO_FLUVIUS_HALLE_VILVOORDE, "Fluvius Halle-Vilvoorde"),
        (DSO_FLUVIUS_IMEWO, "Fluvius Imewo"),
        (DSO_FLUVIUS_INTERGEM, "Fluvius Midden-Vlaanderen (Intergem)"),
        (DSO_FLUVIUS_IVEKA, "Fluvius Kempen (Iveka)"),
        (DSO_FLUVIUS_LIMBURG, "Fluvius Limburg"),
        (DSO_FLUVIUS_WEST, "Fluvius West"),
        (DSO_FLUVIUS_ZENNE_DIJLE, "Fluvius Zenne-Dijle"),
    ),
    REGION_WALLONIA: (
        (DSO_AIEG, "AIEG"),
        (DSO_AIESH, "AIESH"),
        (DSO_ORES, "ORES"),
        (DSO_RESA, "RESA"),
        (DSO_REW, "Regie de Wavre"),
    ),
    REGION_BRUSSELS: ((DSO_SIBELGA, "Sibelga"),),
}

CONF_REGION: Final = "region"
CONF_DSO: Final = "dso"
CONF_SUPPLIER: Final = "supplier"
CONF_CONTRACT: Final = "contract"
CONF_METER: Final = "meter"
CONF_API_KEY: Final = "api_key"

METER_MONO: Final = "mono"
METER_BI: Final = "bi"
METER_DYNAMIC: Final = "dynamic"
# Separate exclusive-night meter circuit (electric water heater,
# night-storage heater): the meter only registers consumption during
# DSO off-peak hours and bills at the supplier's published
# exclusive_night rate. Configure as a SECOND config entry pointing at
# the exclusive-night kWh sensor; the primary (day) meter stays on
# mono / bi / dynamic.
METER_EXCLUSIVE_NIGHT: Final = "exclusive_night"

METER_TYPES: Final = (METER_MONO, METER_BI, METER_DYNAMIC, METER_EXCLUSIVE_NIGHT)

# DSO-side billing mode, orthogonal to the supplier meter. Wallonia
# users with a smart meter can opt into "impact" (Tarif Impact, set by
# CWaPE; 3 distribution rates by hour-of-day band). Outside Wallonia
# only "simple" and "bi_horaire" are meaningful; the coordinator falls
# back automatically when the DSO doesn't publish Impact rates.
CONF_DSO_TARIFF_MODE: Final = "dso_tariff_mode"
DSO_MODE_SIMPLE: Final = "simple"
DSO_MODE_BI_HORAIRE: Final = "bi_horaire"
DSO_MODE_IMPACT: Final = "impact"
DSO_TARIFF_MODES: Final = (DSO_MODE_SIMPLE, DSO_MODE_BI_HORAIRE, DSO_MODE_IMPACT)

CONF_CAPACITY_MODE: Final = "capacity_mode"
CONF_CAPACITY_PEAK_SENSOR: Final = "capacity_peak_sensor"
CONF_CAPACITY_FIXED_KW: Final = "capacity_fixed_kw"

# Cumulative kWh meter sensors (HA entity_ids) for the current_year_cost sensor.
# Two ways to feed the sensor:
#   1) Direct day/night registers off the meter (4 entity_ids below).
#      Preferred when available: the bill is computed exactly from the
#      printed meter reading.
#   2) Single cumulative totals (2 entity_ids below). The coordinator
#      reads daily kWh from HA's recorder long-term statistics and, for
#      bi-hourly / SMR3 meters, recovers the day/night split per past
#      day from the recorder's hourly statistics binned via
#      is_offpeak. Useful when the user only has clamp meters /
#      inverter readings without the per-band split. Each side
#      (consumption, injection) is resolved independently; partial
#      register-pair wiring on either side is rejected.
# When both are configured, the day/night registers win.
CONF_DAY_CONSUMPTION_KWH: Final = "day_consumption_kwh"
CONF_NIGHT_CONSUMPTION_KWH: Final = "night_consumption_kwh"
CONF_DAY_INJECTION_KWH: Final = "day_injection_kwh"
CONF_NIGHT_INJECTION_KWH: Final = "night_injection_kwh"
CONF_CONSUMPTION_KWH: Final = "consumption_kwh"
CONF_INJECTION_KWH: Final = "injection_kwh"

# Solar inverter capacity in kVA. 0 means no panels (no prosumer cost).
CONF_SOLAR_KVA: Final = "solar_kva"
CONF_SOLAR_REGIME: Final = "solar_regime"

# Walloon compensation regime ("compteur qui tourne a l'envers") only applies
# to installations certified before 2024-01-01 and stays valid until
# 2030-12-31 (CWaPE / EU directive transition). Newer installations are
# under the injection tariff. Flemish digital meters are SMR3 from the start.
SOLAR_REGIME_NONE: Final = "none"
SOLAR_REGIME_COMPENSATION: Final = "compensation"
SOLAR_REGIME_INJECTION: Final = "injection"
SOLAR_REGIMES: Final = (
    SOLAR_REGIME_NONE,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
)

CAPACITY_MODE_SENSOR: Final = "sensor"
CAPACITY_MODE_FIXED: Final = "fixed"

# Regulated minimum monthly peak that Fluvius bills against in Flanders -
# the user's actual peak is taken as max(measured, floor) before being
# multiplied by capacity_eur_per_kw_year. Set by VREG when the capacity
# tariff was introduced in January 2023 and unchanged since.
VREG_CAPACITY_FLOOR_KW: Final = 2.5

ENTSOE_BASE_URL: Final = "https://web-api.tp.entsoe.eu/api"
ENTSOE_BE_DOMAIN: Final = "10YBE----------2"

# Coordinator refreshes every hour for both static and dynamic contracts;
# the dynamic branch piggybacks on this tick to refresh ENTSO-E spots.
UPDATE_INTERVAL_MINUTES: Final = 60

STORAGE_VERSION: Final = 2

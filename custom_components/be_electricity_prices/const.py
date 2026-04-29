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

PLATFORMS: Final = ("sensor",)

REGION_FLANDERS: Final = "flanders"
REGION_WALLONIA: Final = "wallonia"
REGION_BRUSSELS: Final = "brussels"

REGIONS: Final = (REGION_FLANDERS, REGION_WALLONIA, REGION_BRUSSELS)

# DSO selection per region. Flanders has eight Fluvius sub-areas with
# materially different distribution rates; Wallonia DSOs are uniform per
# operator; Brussels has one (Sibelga).
DSO_CHOICES: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    REGION_FLANDERS: (
        ("fluvius_antwerpen", "Fluvius Antwerpen"),
        ("fluvius_halle_vilvoorde", "Fluvius Halle-Vilvoorde"),
        ("fluvius_imewo", "Fluvius Imewo"),
        ("fluvius_intergem", "Fluvius Midden-Vlaanderen (Intergem)"),
        ("fluvius_iveka", "Fluvius Kempen (Iveka)"),
        ("fluvius_limburg", "Fluvius Limburg"),
        ("fluvius_west", "Fluvius West"),
        ("fluvius_zenne_dijle", "Fluvius Zenne-Dijle"),
    ),
    REGION_WALLONIA: (
        ("aieg", "AIEG"),
        ("aiesh", "AIESH"),
        ("ores", "ORES"),
        ("resa", "RESA"),
        ("rew", "Regie de Wavre"),
    ),
    REGION_BRUSSELS: (("sibelga", "Sibelga"),),
}

TARIFF_FIXED: Final = "fixed"
TARIFF_VARIABLE: Final = "variable"
TARIFF_DYNAMIC: Final = "dynamic"

CONF_REGION: Final = "region"
CONF_DSO: Final = "dso"
CONF_SUPPLIER: Final = "supplier"
CONF_CONTRACT: Final = "contract"
CONF_METER: Final = "meter"
CONF_API_KEY: Final = "api_key"

METER_MONO: Final = "mono"
METER_BI: Final = "bi"
METER_DYNAMIC: Final = "dynamic"

METER_TYPES: Final = (METER_MONO, METER_BI, METER_DYNAMIC)

CONF_CAPACITY_MODE: Final = "capacity_mode"
CONF_CAPACITY_PEAK_SENSOR: Final = "capacity_peak_sensor"
CONF_CAPACITY_FIXED_KW: Final = "capacity_fixed_kw"

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

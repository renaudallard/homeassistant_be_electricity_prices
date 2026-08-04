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

PLATFORMS: Final = ("sensor", "binary_sensor", "button")

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

# Fluvius sub-area names as most suppliers print them in their Flanders
# DSO table (Title case, hyphenated), mapped to the DSO key. Shared by the
# providers whose cards use this exact spelling; suppliers that abbreviate
# or upper-case (Bolt "Midden-Vl", Engie all-caps, EBEM unhyphenated) keep
# their own label map.
FLUVIUS_CARD_LABELS: Final[dict[str, str]] = {
    "Fluvius Antwerpen": DSO_FLUVIUS_ANTWERPEN,
    "Fluvius Halle-Vilvoorde": DSO_FLUVIUS_HALLE_VILVOORDE,
    "Fluvius Imewo": DSO_FLUVIUS_IMEWO,
    "Fluvius Kempen": DSO_FLUVIUS_IVEKA,
    "Fluvius Limburg": DSO_FLUVIUS_LIMBURG,
    "Fluvius Midden-Vlaanderen": DSO_FLUVIUS_INTERGEM,
    "Fluvius West": DSO_FLUVIUS_WEST,
    "Fluvius Zenne-Dijle": DSO_FLUVIUS_ZENNE_DIJLE,
}

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
# Optional contract lifecycle dates, stored as ISO "YYYY-MM-DD" strings (the
# DateSelector return value). CONF_CONTRACT_START_DATE prices a fixed/dynamic
# contract at the rate locked in its signing month instead of the current card
# (see the coordinator cohort-energy path); CONF_CONTRACT_END_DATE is inert for
# pricing and only surfaced as a renewal-reminder timestamp sensor.
CONF_CONTRACT_START_DATE: Final = "contract_start_date"
CONF_CONTRACT_END_DATE: Final = "contract_end_date"

# Optional manual signing-rate override, offered on the config flow when a
# start date is set on a fixed / dynamic contract. Used as the cohort energy
# leg when the supplier keeps no archive of the signing month (or the archive
# does not reach that far back), where the rate cannot be retrieved and only
# the user knows what they locked in. Fixed contracts fill single (+ optional
# peak / offpeak / exclusive night); dynamic contracts fill factor / base. A
# dedicated night circuit bills its own rate, so it needs its own box or the
# card's night rate keeps billing whatever else was typed. Per-kWh values are
# entered in the supplier card's VAT basis (grossed at compute time); the
# yearly fee is entered VAT-inclusive, matching how cards store it.
CONF_MANUAL_ENERGY_SINGLE: Final = "manual_energy_single"
CONF_MANUAL_ENERGY_PEAK: Final = "manual_energy_peak"
CONF_MANUAL_ENERGY_OFFPEAK: Final = "manual_energy_offpeak"
CONF_MANUAL_ENERGY_EXCLUSIVE_NIGHT: Final = "manual_energy_exclusive_night"
CONF_MANUAL_ENERGY_FACTOR: Final = "manual_energy_factor"
CONF_MANUAL_ENERGY_BASE: Final = "manual_energy_base"
CONF_MANUAL_YEARLY_FEE: Final = "manual_yearly_fee"

CONF_METER: Final = "meter"
CONF_API_KEY: Final = "api_key"

# Whether prices include VAT. Only meaningful on a contract whose card
# prints excluding VAT (the professional cards); residential cards print
# VAT-inclusive and the setting cannot change them. A business that
# deducts VAT bears the ex-VAT cost and sets this False; one that cannot
# deduct (or a private customer) leaves it True.
CONF_INCLUDE_VAT: Final = "include_vat"
DEFAULT_INCLUDE_VAT: Final = True

# Estimated yearly consumption in kWh, used to pick the federal excise band
# on a card that prints the special excise as a degressive schedule instead
# of one rate (the professional cards). Inert on a residential card. The
# default is the same 3500 kWh household figure the compare page assumes
# when no kWh sensor is wired.
CONF_ANNUAL_CONSUMPTION_KWH: Final = "annual_consumption_kwh"
DEFAULT_ANNUAL_CONSUMPTION_KWH: Final = 3500.0

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

# Brussels contractual connection power (kVA), used only to pick the Brugel
# OSP (Obligations de Service Public) annual-fee tier off the Sibelga card.
# Residential connections are <=13 kVA, so only the four residential tiers
# are offered; the key is matched against the parsed OSP table.
CONF_CONNECTION_KVA_TIER: Final = "connection_kva_tier"
CONNECTION_KVA_TIER_LE1_44: Final = "le1_44"
CONNECTION_KVA_TIER_LE6: Final = "le6"
CONNECTION_KVA_TIER_LE9_6: Final = "le9_6"
CONNECTION_KVA_TIER_LE13: Final = "le13"
CONNECTION_KVA_TIERS: Final = (
    CONNECTION_KVA_TIER_LE1_44,
    CONNECTION_KVA_TIER_LE6,
    CONNECTION_KVA_TIER_LE9_6,
    CONNECTION_KVA_TIER_LE13,
)
DEFAULT_CONNECTION_KVA_TIER: Final = CONNECTION_KVA_TIER_LE6

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

# Spot-price grid resolution. ENTSO-E publishes the Belgian day-ahead
# curve at 15-minute granularity since the SDAC 15-min MTU go-live
# (2025-10-01). The integration aggregates to hourly by default and keeps
# the native quarter-hour slots only for suppliers that actually bill per
# quarter-hour (Engie Dynamic). Values match the ENTSO-E resolution
# tokens so the spot client can reuse them.
RESOLUTION_HOURLY: Final = "PT60M"
RESOLUTION_QUARTER: Final = "PT15M"

# Coordinator refreshes every hour for both static and dynamic contracts;
# the dynamic branch piggybacks on this tick to refresh ENTSO-E spots.
UPDATE_INTERVAL_MINUTES: Final = 60

STORAGE_VERSION: Final = 2

# --- Expert custom-formula supplier ------------------------------------------
# An escape hatch for suppliers that publish no public, machine-resolvable
# tariff card (e.g. Yuso, the Mega iChoosr / Samen Overstappen groepsaankoop),
# so the normal scrape-a-card path is impossible. The user types their own
# commodity formula and all regulated DSO + tax values; the coordinator builds
# the snapshot locally from the config entry instead of fetching it. Surfaced
# last in the supplier dropdown and labelled as an expert option.
SUPPLIER_CUSTOM: Final = "custom"

# One contract per energy mode (the contract step doubles as the mode picker).
CUSTOM_CONTRACT_DYNAMIC: Final = "custom_dynamic"  # factor * live spot + base
CUSTOM_CONTRACT_MONTHLY: Final = "custom_monthly"  # factor * monthly-mean spot + base
CUSTOM_CONTRACT_FIXED: Final = "custom_fixed"  # flat manual rate
CUSTOM_CONTRACTS: Final = (
    CUSTOM_CONTRACT_DYNAMIC,
    CUSTOM_CONTRACT_MONTHLY,
    CUSTOM_CONTRACT_FIXED,
)

# Energy formula inputs (interpretation depends on the chosen contract).
CONF_CUSTOM_ENERGY_FACTOR: Final = "custom_energy_factor"
CONF_CUSTOM_ENERGY_BASE: Final = "custom_energy_base"
CONF_CUSTOM_ENERGY_QUARTER_HOURLY: Final = "custom_energy_quarter_hourly"
CONF_CUSTOM_ENERGY_SINGLE: Final = "custom_energy_single"
CONF_CUSTOM_ENERGY_PEAK: Final = "custom_energy_peak"
CONF_CUSTOM_ENERGY_OFFPEAK: Final = "custom_energy_offpeak"
CONF_CUSTOM_ENERGY_EXCLUSIVE_NIGHT: Final = "custom_energy_exclusive_night"
CONF_CUSTOM_YEARLY_FIXED_FEE: Final = "custom_yearly_fixed_fee"

# Injection formula inputs. "current" = a flat EUR/kWh value; "formula" =
# factor/base applied against the live spot (dynamic) or the monthly mean
# (monthly-average), floored at zero when the guarantee forbids negatives.
CONF_CUSTOM_INJECTION_MODE: Final = "custom_injection_mode"
CUSTOM_INJECTION_MODE_CURRENT: Final = "current"
CUSTOM_INJECTION_MODE_FORMULA: Final = "formula"
CUSTOM_INJECTION_MODES: Final = (
    CUSTOM_INJECTION_MODE_CURRENT,
    CUSTOM_INJECTION_MODE_FORMULA,
)
CONF_CUSTOM_INJECTION_CURRENT: Final = "custom_injection_current"
CONF_CUSTOM_INJECTION_FACTOR: Final = "custom_injection_factor"
CONF_CUSTOM_INJECTION_BASE: Final = "custom_injection_base"
CONF_CUSTOM_INJECTION_FLOOR: Final = "custom_injection_floor"
# Opt-in for the monthly-average mode: weight the injection month-mean by the
# Synergrid solar production profile (SPP) instead of a plain arithmetic mean,
# matching SPP-indexed contracts. Fetched live; falls back to the plain mean.
CONF_CUSTOM_INJECTION_SPP_WEIGHTED: Final = "custom_injection_spp_weighted"

# Regulated DSO network overlay, entered by hand (region/meter-relevant fields
# only; all but distribution_single default to 0.0). Maps onto DsoOverlay.
CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: Final = "custom_dso_distribution_single"
CONF_CUSTOM_DSO_DISTRIBUTION_PEAK: Final = "custom_dso_distribution_peak"
CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK: Final = "custom_dso_distribution_offpeak"
CONF_CUSTOM_DSO_DISTRIBUTION_EXCLUSIVE_NIGHT: Final = (
    "custom_dso_distribution_exclusive_night"
)
CONF_CUSTOM_DSO_TRANSPORT: Final = "custom_dso_transport"
CONF_CUSTOM_DSO_DATA_MANAGEMENT_PER_YEAR: Final = "custom_dso_data_management_per_year"
CONF_CUSTOM_DSO_CAPACITY_EUR_PER_KW_YEAR: Final = "custom_dso_capacity_eur_per_kw_year"
CONF_CUSTOM_DSO_PROSUMER_EUR_PER_KVA_YEAR: Final = (
    "custom_dso_prosumer_eur_per_kva_year"
)
CONF_CUSTOM_DSO_DISTRIBUTION_PIC: Final = "custom_dso_distribution_pic"
CONF_CUSTOM_DSO_DISTRIBUTION_MEDIUM: Final = "custom_dso_distribution_medium"
CONF_CUSTOM_DSO_DISTRIBUTION_ECO: Final = "custom_dso_distribution_eco"
CONF_CUSTOM_DSO_BRUSSELS_OSP: Final = "custom_dso_brussels_osp"

# Regulated taxes/levies overlay, entered by hand. Maps onto TaxOverlay; the
# single regional-renewables field fills the region's slot at build time.
CONF_CUSTOM_TAX_FEDERAL_EXCISE: Final = "custom_tax_federal_excise"
CONF_CUSTOM_TAX_ENERGY_CONTRIBUTION: Final = "custom_tax_energy_contribution"
CONF_CUSTOM_TAX_REGIONAL_RENEWABLES: Final = "custom_tax_regional_renewables"
CONF_CUSTOM_TAX_REGION_CONNECTION_FEE: Final = "custom_tax_region_connection_fee"
CONF_CUSTOM_TAX_ENERGY_FUND_PER_MONTH: Final = "custom_tax_energy_fund_per_month"
# VAT rate the pricing engine grosses up per component (energy/network/taxes;
# injection stays exempt). Default 0.06 so users type the excl-VAT coefficients
# printed on their tariff sheet verbatim.
CONF_CUSTOM_VAT_RATE: Final = "custom_vat_rate"
DEFAULT_CUSTOM_VAT_RATE: Final = 0.06

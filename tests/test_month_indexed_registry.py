"""The registry's month_indexed_energy flag against what the parsers say.

The flow offers the optional ENTSO-E key from the registry flag, before any
card is fetched; the coordinator re-prices from the parsed ``month_indexed``.
Where they disagree, a flagged card with no formula offers a key nothing
resolves and a formula with no flag is a re-price no flow step can switch on,
which is how Eneco Flex sat unrepriced. Every fixture is held to it here and
every live card in the live check."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from custom_components.be_electricity_prices.const import (
    REGION_FLANDERS,
    REGION_WALLONIA,
    SPOT_PRICED_CONTRACT_KINDS,
)
from custom_components.be_electricity_prices.flow_contracts import (
    _contract_is_month_indexed,
)
from custom_components.be_electricity_prices.providers import (
    EXTRACTORS,
    cociter,
    ebem,
    eneco,
    engie,
    luminus,
    mega,
    octaplus,
    totalenergies,
)
from custom_components.be_electricity_prices.providers._pdf import (
    extract_pdf_text_aligned,
)
from custom_components.be_electricity_prices.providers.base import SupplierSnapshot
from tests import FIXTURES, fixture_text


def _aligned(name: str) -> str:
    return extract_pdf_text_aligned(
        (FIXTURES / name).read_bytes(), x_join_threshold=1.0
    )


_CASES: list[tuple[str, str, Callable[[], SupplierSnapshot]]] = [
    (
        "cociter",
        "cociter_variable",
        lambda: cociter.parse_snapshot(
            fixture_text("cociter_var_2609.pdf"), "cociter_variable", "t", "2026-09"
        ),
    ),
    (
        "cociter",
        "cociter_variable_impact",
        lambda: cociter.parse_snapshot(
            fixture_text("cociter_vai_2609.pdf"),
            "cociter_variable_impact",
            "t",
            "2026-09",
        ),
    ),
    (
        "cociter",
        "cociter_dynamic",
        lambda: cociter.parse_snapshot(
            fixture_text("cociter_dyn_2604.pdf"), "cociter_dynamic", "t", "2026-04"
        ),
    ),
    (
        "ebem",
        "ebem_variable",
        lambda: ebem.parse_snapshot(
            "ebem_variable",
            fixture_text("ebem_variable_2026-05.pdf", layout=True),
            "t://v",
            "2026-05",
        ),
    ),
    (
        "ebem",
        "ebem_dynamic",
        lambda: ebem.parse_snapshot(
            "ebem_dynamic",
            fixture_text("ebem_dynamic_2026-05.pdf", layout=True),
            "t://v",
            "2026-05",
        ),
    ),
    (
        "engie",
        "engie_empower_variable",
        lambda: engie.parse_snapshot(
            "engie_empower_variable",
            {REGION_WALLONIA: fixture_text("engie_empower_flextime_w.pdf")},
        ),
    ),
    (
        "engie",
        "engie_empower_flextime",
        lambda: engie.parse_snapshot(
            "engie_empower_flextime",
            {REGION_WALLONIA: fixture_text("engie_empower_flextime_w.pdf")},
        ),
    ),
    (
        "engie",
        "engie_easy_variable",
        lambda: engie.parse_snapshot(
            "engie_easy_variable",
            {REGION_FLANDERS: fixture_text("engie_easy_indexed_v.pdf")},
        ),
    ),
    (
        "engie",
        "engie_easy_fixed",
        lambda: engie.parse_snapshot(
            "engie_easy_fixed",
            {REGION_FLANDERS: fixture_text("engie_easy_fixed_v.pdf")},
        ),
    ),
    (
        "engie",
        "engie_empty_house",
        lambda: engie.parse_snapshot(
            "engie_empty_house",
            {REGION_FLANDERS: fixture_text("engie_empty_house_v.pdf")},
        ),
    ),
    (
        "engie",
        "engie_dynamic",
        lambda: engie.parse_snapshot(
            "engie_dynamic", {REGION_FLANDERS: fixture_text("engie_dynamic_v.pdf")}
        ),
    ),
    (
        "engie",
        "engie_pro_empower_variable",
        lambda: engie.parse_snapshot(
            "engie_pro_empower_variable",
            {REGION_FLANDERS: fixture_text("engie_pro_empower_variable_v.pdf")},
        ),
    ),
    (
        "engie",
        "engie_pro_empower_flextime",
        lambda: engie.parse_snapshot(
            "engie_pro_empower_flextime",
            {REGION_FLANDERS: fixture_text("engie_pro_empower_variable_v.pdf")},
        ),
    ),
    (
        "engie",
        "engie_pro_easy_variable",
        lambda: engie.parse_snapshot(
            "engie_pro_easy_variable",
            {REGION_WALLONIA: fixture_text("engie_pro_easy_indexed_w.pdf")},
        ),
    ),
    (
        "eneco",
        "power_flex",
        lambda: eneco.parse_snapshot(
            fixture_text("eneco_flex_aug26.pdf"), "power_flex", "t", REGION_WALLONIA
        ),
    ),
    (
        "eneco",
        "power_flex_one",
        lambda: eneco.parse_snapshot(
            fixture_text("eneco_flex_one.pdf"), "power_flex_one", "t", REGION_WALLONIA
        ),
    ),
    (
        "eneco",
        "power_fix",
        lambda: eneco.parse_snapshot(
            fixture_text("eneco_fix.pdf"), "power_fix", "t", REGION_WALLONIA
        ),
    ),
    (
        "eneco",
        "power_dynamic",
        lambda: eneco.parse_snapshot(
            fixture_text("eneco_dyn.pdf"), "power_dynamic", "t", REGION_FLANDERS
        ),
    ),
    (
        "luminus",
        "luminus_maxxflex",
        lambda: luminus.parse_snapshot(
            "luminus_maxxflex", fixture_text("luminus_maxxflex_w.pdf"), REGION_WALLONIA
        ),
    ),
    (
        "luminus",
        "luminus_smartflex",
        lambda: luminus.parse_snapshot(
            "luminus_smartflex",
            fixture_text("luminus_smartflex_w.pdf"),
            REGION_WALLONIA,
        ),
    ),
    (
        "luminus",
        "luminus_comfyflex",
        lambda: luminus.parse_snapshot(
            "luminus_comfyflex",
            fixture_text("luminus_comfyflex_v.pdf"),
            REGION_FLANDERS,
        ),
    ),
    (
        "luminus",
        "luminus_comfyflex_plus",
        lambda: luminus.parse_snapshot(
            "luminus_comfyflex_plus",
            fixture_text("luminus_comfyflex_plus_w.pdf"),
            REGION_WALLONIA,
        ),
    ),
    (
        "luminus",
        "luminus_comfy",
        lambda: luminus.parse_snapshot(
            "luminus_comfy", fixture_text("luminus_comfy_w.pdf"), REGION_WALLONIA
        ),
    ),
    (
        "octaplus",
        "octaplus_smartvariable",
        lambda: octaplus.parse_snapshot(
            "octaplus_smartvariable",
            _aligned("octaplus_smartvariable_w.pdf"),
            REGION_WALLONIA,
        ),
    ),
    (
        "octaplus",
        "octaplus_fixed",
        lambda: octaplus.parse_snapshot(
            "octaplus_fixed", _aligned("octaplus_fixed_w.pdf"), REGION_WALLONIA
        ),
    ),
    (
        "octaplus",
        "octaplus_fixed_impact",
        lambda: octaplus.parse_snapshot(
            "octaplus_fixed_impact", _aligned("octaplus_fixed_w.pdf"), REGION_WALLONIA
        ),
    ),
    (
        "mega",
        "mega_smart_flex",
        lambda: mega.parse_snapshot(
            "mega_smart_flex", fixture_text("mega_smart_flex_w.pdf"), REGION_WALLONIA
        ),
    ),
    (
        "mega",
        "mega_offpeak_impact_var",
        lambda: mega.parse_snapshot(
            "mega_offpeak_impact_var",
            fixture_text("mega_offpeak_impact_w.pdf"),
            REGION_WALLONIA,
        ),
    ),
    (
        "mega",
        "mega_smart_fixed",
        lambda: mega.parse_snapshot(
            "mega_smart_fixed", fixture_text("mega_smart_fixed_w.pdf"), REGION_WALLONIA
        ),
    ),
    (
        "totalenergies",
        "totalenergies_mycomfort",
        lambda: totalenergies.parse_snapshot(
            "totalenergies_mycomfort",
            fixture_text("totalenergies_mycomfort_v.pdf", layout=True),
            REGION_FLANDERS,
        ),
    ),
    (
        "totalenergies",
        "totalenergies_mycomfort_fixed",
        lambda: totalenergies.parse_snapshot(
            "totalenergies_mycomfort_fixed",
            fixture_text("totalenergies_mycomfort_fixed_w.pdf", layout=True),
            REGION_WALLONIA,
        ),
    ),
    (
        "totalenergies",
        "totalenergies_impact",
        lambda: totalenergies.parse_snapshot(
            "totalenergies_impact",
            fixture_text("totalenergies_impact_w.pdf", layout=True),
            REGION_WALLONIA,
        ),
    ),
]


@pytest.mark.parametrize(
    ("supplier", "contract_id", "parse"), _CASES, ids=[c[1] for c in _CASES]
)
def test_registry_flag_matches_what_the_card_parses(
    supplier: str, contract_id: str, parse: Callable[[], SupplierSnapshot]
) -> None:
    contract = next(c for c in EXTRACTORS[supplier].contracts if c.id == contract_id)
    parsed = bool(getattr(parse().energy, "month_indexed", False))
    assert parsed == contract.month_indexed_energy, (
        f"{contract_id}: card parses month_indexed={parsed}, "
        f"registry says {contract.month_indexed_energy}"
    )


def test_every_supplier_that_flags_a_card_is_in_the_case_list() -> None:
    """The case list above is hand written, so what it does NOT cover is
    invisible: 34 contracts carry the flag and the list pins seventeen of them.

    Per-contract coverage is not on offer offline, because a fixture is a card
    somebody saved. What is on offer is per SUPPLIER, and that is where this
    breaks in practice: five suppliers gate the flag on a hand-maintained id
    set and two set it from the kind, so a wrong predicate takes the whole
    supplier with it. TotalEnergies is the precedent, with the twin unset on
    all nine of its contracts until the nightly check found it.

    So every supplier with a flagged contract owes this list one case, and
    one of an unflagged contract where it sells one, which is what makes a
    predicate that answers True to everything fail here rather than in the
    morning's CI issue.
    """
    covered: dict[str, set[bool]] = {}
    for supplier, contract_id, _parse in _CASES:
        contract = next(
            c for c in EXTRACTORS[supplier].contracts if c.id == contract_id
        )
        covered.setdefault(supplier, set()).add(contract.month_indexed_energy)

    for supplier, extractor in sorted(EXTRACTORS.items()):
        flags = {c.month_indexed_energy for c in extractor.contracts}
        if True not in flags:
            continue
        seen = covered.get(supplier, set())
        assert True in seen, (
            f"{supplier} flags a card month-indexed and the case list pins "
            "none of its contracts against a parsed card"
        )
        if False in flags:
            assert False in seen, (
                f"{supplier} sells an unflagged card too and the case list "
                "pins none, so a predicate answering True to everything passes"
            )


def test_month_indexed_energy_is_never_set_on_a_spot_priced_kind() -> None:
    """Dynamic and spot-monthly kinds collect a mandatory key already; the
    flag is for the kinds that would otherwise never be asked."""
    flagged = {
        (ex.id, c.id): c.kind
        for ex in EXTRACTORS.values()
        for c in ex.contracts
        if c.month_indexed_energy
    }
    assert flagged, "no contract carries the flag"
    assert all(kind not in SPOT_PRICED_CONTRACT_KINDS for kind in flagged.values())
    assert {"eneco"} <= {supplier for supplier, _ in flagged}


def test_contract_is_month_indexed_reads_the_registry() -> None:
    assert _contract_is_month_indexed("eneco", "power_flex")
    assert _contract_is_month_indexed("cociter", "cociter_variable_impact")
    assert not _contract_is_month_indexed("eneco", "power_fix")
    assert not _contract_is_month_indexed("nobody", "power_flex")
    assert not _contract_is_month_indexed(None, None)


# How far apart the bands may solve, in EUR/kWh. Sized on the slip it has to
# catch, not on tariff economics: the cards print four decimals of EUR/kWh and
# round each band separately, which is worth up to 1,0e-04 across the fixtures,
# while the SMALLEST real defect (one band's base read on the ex-VAT basis)
# moves a Luminus band by 1,2e-03. This sits between the two, and the test
# below proves it by re-solving a band on the wrong basis and requiring the
# bound to fire.
_MAX_INDEX_SPREAD = 5e-4


# (printed-rate attribute, factor attribute, base attribute) per rate shape.
# A card prints its resolved rates and, beside them, the formula they were
# computed from; these are the pairs that have to agree.
_BAND_ATTRS: dict[str, list[tuple[str, str, str]]] = {
    "TimeOfUseRates": [
        (band, f"formula_factor_{band}", f"formula_base_{band}")
        for band in ("peak", "transition", "offpeak")
    ],
    "ImpactRates": [
        (band, f"{band}_factor", f"{band}_base") for band in ("pic", "medium", "eco")
    ],
    "VariableRates": [
        ("current", "formula_factor", "formula_base"),
        ("peak", "formula_factor_peak", "formula_base_peak"),
        ("offpeak", "formula_factor_offpeak", "formula_base_offpeak"),
        (
            "exclusive_night",
            "formula_factor_exclusive_night",
            "formula_base_exclusive_night",
        ),
    ],
}


@pytest.mark.parametrize(
    ("supplier", "contract_id", "parse"), _CASES, ids=[c[1] for c in _CASES]
)
def test_every_band_solves_to_the_same_month_index(
    supplier: str, contract_id: str, parse: Callable[[], SupplierSnapshot]
) -> None:
    """One card prints ONE month's index, so every band must solve to it.

    Each band carries a printed rate and the ``factor``/``base`` it was
    computed from, so ``(rate - base) / factor`` recovers the index. All of
    them have to land on the same number, and that is the only check that
    holds the coefficients against the rates they came from: a base read off
    the neighbouring column, a factor missing its VAT conversion or a x10 slip
    leaves every individual figure plausible and breaks only the agreement.

    It also covers the fields nothing else names -- ``formula_base_transition``
    was reachable by no assertion in the suite at all, while its factor
    sibling was pinned.
    """
    energy = parse().energy
    if not getattr(energy, "month_indexed", False):
        pytest.skip("not month-indexed; no coefficients to cross-check")
    implied: dict[str, float] = {}
    for rate_attr, factor_attr, base_attr in _BAND_ATTRS[type(energy).__name__]:
        rate = getattr(energy, rate_attr, None)
        factor = getattr(energy, factor_attr, None)
        base = getattr(energy, base_attr, None)
        if rate is None or base is None or not factor:
            continue
        implied[rate_attr] = (rate - base) / factor
    assert implied, f"{contract_id}: month-indexed but no band solves"
    spread = max(implied.values()) - min(implied.values())
    assert spread < _MAX_INDEX_SPREAD, (
        f"{contract_id}: bands disagree on the month index, "
        + ", ".join(f"{k}={v:.6f}" for k, v in implied.items())
    )
    if len(implied) < 2:
        return
    # And the bound is only worth its name if it still fires. Re-solve one
    # band with its base read on the wrong VAT basis (the smallest of the
    # slips this exists to catch) and require the spread to clear it.
    band, factor_attr, base_attr = next(
        (b, f, x)
        for b, f, x in _BAND_ATTRS[type(energy).__name__]
        if b in implied and getattr(energy, x)
    )
    rate = getattr(energy, band)
    ex_vat_base = getattr(energy, base_attr) / 1.06
    perturbed = dict(implied)
    perturbed[band] = (rate - ex_vat_base) / getattr(energy, factor_attr)
    assert max(perturbed.values()) - min(perturbed.values()) > _MAX_INDEX_SPREAD, (
        f"{contract_id}: the bound is too loose to catch a VAT-basis slip"
    )

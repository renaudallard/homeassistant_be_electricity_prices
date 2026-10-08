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

"""icons.json names only entities and services that exist, and gives an
icon to every entity whose device class gives it none."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from homeassistant.helpers.entity import Entity

from custom_components.be_electricity_prices import binary_sensor, button, sensor

PACKAGE = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "be_electricity_prices"
)


def _icons() -> dict[str, Any]:
    with (PACKAGE / "icons.json").open(encoding="utf-8") as fp:
        data: dict[str, Any] = json.load(fp)
    return data


def _entities(module: Any) -> dict[str, bool]:
    """Every translation key a platform module creates an entity under,
    against whether it carries a device class."""
    out: dict[str, bool] = {}
    for value in vars(module).values():
        found = value if isinstance(value, tuple) else (value,)
        for item in found:
            # Duck-typed: Home Assistant rebuilds its frozen description
            # classes, so isinstance cannot be trusted across them.
            if (
                not isinstance(item, type)
                and hasattr(item, "key")
                and isinstance(getattr(item, "translation_key", None), str)
            ):
                out[item.translation_key] = (
                    getattr(item, "device_class", None) is not None
                )
            elif isinstance(item, type) and issubclass(item, Entity):
                key = getattr(item, "_attr_translation_key", None)
                if isinstance(key, str):
                    out[key] = getattr(item, "_attr_device_class", None) is not None
    return out


def test_every_entity_icon_names_an_entity_and_none_is_missing() -> None:
    for name, module in (
        ("sensor", sensor),
        ("binary_sensor", binary_sensor),
        ("button", button),
    ):
        entities = _entities(module)
        named = set(_icons()["entity"].get(name, {}))
        assert named <= set(entities), (name, named - set(entities))
        # One with a device class keeps the icon Home Assistant gives it.
        plain = {key for key, has_class in entities.items() if not has_class}
        assert named == plain, (name, plain ^ named)


def test_every_service_has_an_icon() -> None:
    with (PACKAGE / "services.yaml").open(encoding="utf-8") as fp:
        services = set(yaml.safe_load(fp))
    assert set(_icons()["services"]) == services

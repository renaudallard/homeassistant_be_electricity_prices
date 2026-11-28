"""The Home Assistant test helpers, from whichever harness is installed.

Home Assistant ships its own harness, pytest-homeassistant, from 2026.11 on,
and pytest-homeassistant-custom-component stops at 2026.10. The suite runs on
the release hacs.json names and on a current one, so the tests take the
helpers from here rather than from either package.
"""

try:
    from pytest_homeassistant.common import (
        CLIENT_ID,
        MockConfigEntry,
        MockEntityPlatform,
        async_fire_time_changed,
    )
    from pytest_homeassistant.components.recorder.common import (
        async_wait_recording_done,
        do_adhoc_statistics,
    )
except ImportError:
    from pytest_homeassistant_custom_component.common import (
        CLIENT_ID,
        MockConfigEntry,
        MockEntityPlatform,
        async_fire_time_changed,
    )
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
        do_adhoc_statistics,
    )

__all__ = [
    "CLIENT_ID",
    "MockConfigEntry",
    "MockEntityPlatform",
    "async_fire_time_changed",
    "async_wait_recording_done",
    "do_adhoc_statistics",
]

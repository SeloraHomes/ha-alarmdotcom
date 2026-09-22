"""
Momentary open/close handling for contact sensors.

Alarm.com collapses a quick open-then-close into a single OpenedClosed event
(type 100). Writing its literal SensorState.OPENED_CLOSED (9) made the opening
invisible in Home Assistant: on/off is derived from state parity and 9 is odd,
so the sensor read closed, never changed state, and left nothing in history for
an automation to see. Confirmed on a real system - a five-second front door
open produced no entry at all, while an open held for 30 seconds (a plain
Opened, type 15) showed up immediately.
"""

from __future__ import annotations

# isort: skip_file
# Import order matters here and must not be sorted: importing the component
# runs its sys.path shim, which puts the vendored library on sys.path as
# top-level `_pyalarmdotcomajax` - the same name the vendored modules import
# each other by.
import custom_components.alarmdotcom  # noqa: F401

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from _pyalarmdotcomajax.const import ATTR_STATE
from _pyalarmdotcomajax.controllers.sensors import (
    SENSOR_EVENT_STATE_MAP,
    SensorController,
)
from _pyalarmdotcomajax.models.jsonapi import Resource
from _pyalarmdotcomajax.models.sensor import Sensor, SensorState, SensorSubtype
from _pyalarmdotcomajax.websocket.client import RawResourceEventMessage
from _pyalarmdotcomajax.websocket.messages import EventWSMessage, ResourceEventType


def _sensor(subtype: SensorSubtype, state: SensorState = SensorState.CLOSED) -> Sensor:
    """Build a Sensor stub carrying only what the pulse path touches."""
    resource = MagicMock(spec=Sensor)
    resource.id = "1234-5"
    resource.subtype = subtype
    resource.attributes = SimpleNamespace(state=state)
    resource.api_resource = SimpleNamespace(attributes={})
    return resource


def _event(subtype: ResourceEventType) -> EventWSMessage:
    message = MagicMock(spec=EventWSMessage)
    message.subtype = subtype
    message.value = None
    return message


def _controller(resource: Sensor) -> SensorController:
    """Build a controller real enough to run the pulse, faked everywhere else."""
    controller = MagicMock(spec=SensorController)
    controller._opened_closed_pulses = {}
    # Created in BaseController.__init__, so not part of the class spec.
    controller._background_tasks = set()
    controller.get = MagicMock(return_value=resource)
    controller._register_or_update_resource = AsyncMock(return_value=None)

    # The methods under test run for real; everything else stays mocked.
    controller._cancel_opened_closed_settle = (
        lambda resource_id: SensorController._cancel_opened_closed_settle(
            controller, resource_id
        )
    )
    controller._schedule_opened_closed_settle = (
        lambda resource_id, settled, opened: (
            SensorController._schedule_opened_closed_settle(
                controller, resource_id, settled, opened
            )
        )
    )
    controller._settle_opened_closed = (
        lambda resource_id, settled, opened: SensorController._settle_opened_closed(
            controller, resource_id, settled, opened
        )
    )

    return controller


def test_opened_closed_maps_to_open_not_to_the_unreadable_state() -> None:
    """
    OPENED_CLOSED (9) is odd, and odd reads as "closed" downstream.

    Mapping the event to OPEN is what makes the opening visible at all.
    """
    assert SENSOR_EVENT_STATE_MAP[ResourceEventType.OpenedClosed] == SensorState.OPEN


@pytest.mark.asyncio
async def test_opened_closed_opens_immediately_then_settles_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A momentary open is replayed as open now, closed a moment later."""
    monkeypatch.setattr(
        "_pyalarmdotcomajax.controllers.sensors.OPENED_CLOSED_PULSE_S", 0
    )

    resource = _sensor(SensorSubtype.CONTACT_SENSOR)
    controller = _controller(resource)

    updated = await SensorController._handle_event(
        controller, resource, _event(ResourceEventType.OpenedClosed)
    )

    # Open is written and published straight away.
    assert updated.api_resource.attributes[ATTR_STATE] == SensorState.OPEN.value

    # The pulse settles it back once the window elapses.
    resource.attributes.state = SensorState.OPEN
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert resource.api_resource.attributes[ATTR_STATE] == SensorState.CLOSED.value
    controller._register_or_update_resource.assert_awaited_once()


@pytest.mark.asyncio
async def test_motion_sensor_pulse_uses_active_and_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Motion sensors have no open/closed - they go active, then idle."""
    monkeypatch.setattr(
        "_pyalarmdotcomajax.controllers.sensors.OPENED_CLOSED_PULSE_S", 0
    )

    resource = _sensor(SensorSubtype.MOTION_SENSOR, state=SensorState.IDLE)
    controller = _controller(resource)

    updated = await SensorController._handle_event(
        controller, resource, _event(ResourceEventType.OpenedClosed)
    )

    assert updated.api_resource.attributes[ATTR_STATE] == SensorState.ACTIVE.value

    resource.attributes.state = SensorState.ACTIVE
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert resource.api_resource.attributes[ATTR_STATE] == SensorState.IDLE.value


@pytest.mark.asyncio
async def test_a_newer_event_cancels_a_pending_pulse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A real Opened arriving mid-pulse must not be closed by the older timer.

    Otherwise the sensor would read closed while the door stood open, until
    some later event or refresh corrected it.
    """
    monkeypatch.setattr(
        "_pyalarmdotcomajax.controllers.sensors.OPENED_CLOSED_PULSE_S", 0.05
    )

    resource = _sensor(SensorSubtype.CONTACT_SENSOR)
    controller = _controller(resource)

    await SensorController._handle_event(
        controller, resource, _event(ResourceEventType.OpenedClosed)
    )
    resource.attributes.state = SensorState.OPEN

    # The door is opened for real before the pulse elapses.
    await SensorController._handle_event(
        controller, resource, _event(ResourceEventType.Opened)
    )

    await asyncio.sleep(0.1)

    assert resource.api_resource.attributes[ATTR_STATE] == SensorState.OPEN.value
    controller._register_or_update_resource.assert_not_awaited()


@pytest.mark.asyncio
async def test_pulse_defers_to_a_state_set_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a refresh moved the sensor while the pulse waited, that newer truth wins."""
    monkeypatch.setattr(
        "_pyalarmdotcomajax.controllers.sensors.OPENED_CLOSED_PULSE_S", 0
    )

    resource = _sensor(SensorSubtype.CONTACT_SENSOR)
    controller = _controller(resource)

    await SensorController._handle_event(
        controller, resource, _event(ResourceEventType.OpenedClosed)
    )

    # A full-state refresh lands first, reporting the door as closed already.
    resource.attributes.state = SensorState.CLOSED
    resource.api_resource.attributes[ATTR_STATE] = SensorState.CLOSED.value

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    controller._register_or_update_resource.assert_not_awaited()


#
# End-to-end through the controller's real event path.
#
# The tests above drive _handle_event directly with mocks, which cannot show
# what subscribers actually see. These build a real Sensor from a real JSON:API
# Resource and go through _base_handle_event, so the published state is the
# thing being asserted.
#


def _real_sensor_resource() -> Resource:
    """
    Build a JSON:API resource for the front door.

    Attributes are the real payload from that sensor's diagnostics, not a
    hand-picked subset: the model requires most of them, and a trimmed stub
    would only prove that the stub parses.
    """
    return Resource(
        id="94927580-2",
        type="devices/sensor",
        attributes={
            "addDeviceResource": 0,
            "associatedCameraDeviceIds": {},
            "batteryLevelClassification": None,
            "batteryLevelNull": None,
            "canAccessAppSettings": False,
            "canAccessTroubleshootingWizard": False,
            "canAccessWebSettings": True,
            "canBeAssociatedToVideoDevice": True,
            "canBeDeleted": False,
            "canBeRenamed": True,
            "canBeSaved": True,
            "canChangeDescription": True,
            "canConfirmStateChange": True,
            "canReceiveCommands": False,
            "description": 'Front Door',
            "desiredState": 1,
            "deviceIcon": {'icon': 317},
            "deviceModelId": 110,
            "deviceRole": 0,
            "deviceType": 1,
            "displayStateText": 'Closed',
            "hasPermissionToChangeState": True,
            "hasState": True,
            "isAssignedToCareReceiver": False,
            "isBypassed": False,
            "isFlexIo": False,
            "isMalfunctioning": False,
            "isMatter": False,
            "isMonitoringEnabled": True,
            "isOAuth": False,
            "isZWave": False,
            "isZWaveWakeupNode": False,
            "macAddress": '',
            "managedDeviceType": 14,
            "manufacturer": None,
            "matterAssociatedIdToNameMap": None,
            "matterParentNodeId": None,
            "openClosedStatus": 2,
            "primaryAssociatedDeviceIds": None,
            "remoteCommandsEnabled": True,
            "sensorNamingFormat": 3,
            "showDeletionMessage": False,
            "state": 1,
            "supportsBypass": True,
            "supportsCommandClassBasic": False,
            "supportsImmediateBypass": True,
            "troubleshootingWizard": None,
            "unitSupportsRemovingWakeupNode": False,
            "webSettings": 159,
        },
    )


async def _controller_with_real_resource(
    resource: Resource,
) -> tuple[SensorController, list]:
    """Build a SensorController holding one real sensor, capturing publishes."""
    published: list = []

    bridge = MagicMock()
    bridge.events.publish = published.append

    controller = SensorController(bridge)
    await controller._register_or_update_resource(resource)
    published.clear()  # drop the registration event

    return controller, published


def _raw_event(subtype: ResourceEventType) -> EventWSMessage:
    """Build a websocket event message addressed at the sensor above."""
    return EventWSMessage.from_dict(
        {
            "unit_id": "94927580",
            "device_id": 2,
            "event_type": subtype.value,
            "event_value": 0.0,
            "event_date_utc": "2026-09-22T16:28:00.077Z",
            "qstring_for_extra_data": None,
        }
    )


@pytest.mark.asyncio
async def test_open_is_published_before_the_pulse_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subscribers see Open immediately, then Closed - two separate updates."""
    monkeypatch.setattr(
        "_pyalarmdotcomajax.controllers.sensors.OPENED_CLOSED_PULSE_S", 0
    )

    resource = _real_sensor_resource()
    controller, published = await _controller_with_real_resource(resource)

    await controller._base_handle_event(
        RawResourceEventMessage(ws_message=_raw_event(ResourceEventType.OpenedClosed))
    )

    assert [m.resource.attributes.state for m in published] == [SensorState.OPEN]

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert [m.resource.attributes.state for m in published] == [
        SensorState.OPEN,
        SensorState.CLOSED,
    ]


@pytest.mark.asyncio
async def test_a_refresh_during_the_pulse_is_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A refresh landing mid-pulse wins, even when it also says "open".

    The pulse timer only speaks for the open it wrote itself. A refresh builds
    a new Resource from the API payload, so identity - not the state value -
    is what tells them apart: a door that is genuinely still open must not be
    slammed shut by a timer from two seconds ago.
    """
    monkeypatch.setattr(
        "_pyalarmdotcomajax.controllers.sensors.OPENED_CLOSED_PULSE_S", 0
    )

    resource = _real_sensor_resource()
    controller, _ = await _controller_with_real_resource(resource)

    await controller._base_handle_event(
        RawResourceEventMessage(ws_message=_raw_event(ResourceEventType.OpenedClosed))
    )

    # A full-state refresh arrives, reporting the door as genuinely open.
    refreshed = _real_sensor_resource()
    refreshed.attributes["state"] = SensorState.OPEN.value
    refreshed.attributes["description"] = "Front Door (refreshed)"
    await controller._register_or_update_resource(refreshed)

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert controller.get("94927580-2").attributes.state == SensorState.OPEN

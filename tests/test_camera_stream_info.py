"""
Diagnosing a camera that returns no WebRTC config.

A camera on a live system logged "No WebRTC config found" every refresh, and
the warning listed the response's key *names* without the one field that
explains it. Getting at that meant enabling a separate logger which is pinned
off by default because it prints live session credentials - so the warning has
to carry the reason itself.
"""

from __future__ import annotations

# isort: skip_file
# Import order matters here and must not be sorted: importing the component
# runs its sys.path shim.
import custom_components.alarmdotcom  # noqa: F401

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.alarmdotcom.camera_api import AlarmCameraSession


def _session_returning(body: dict) -> AlarmCameraSession:
    """Build a session whose stream-info request returns this body."""
    session = MagicMock(spec=AlarmCameraSession)

    response = MagicMock()
    response.json = AsyncMock(return_value=body)
    session.get = AsyncMock(return_value=response)

    return session


# The shape a real camera returned when Alarm.com had no stream for it: the
# janus keys are present but empty, and errorEnum carries the reason.
UNAVAILABLE_BODY = {
    "data": {
        "attributes": {
            "errorEnum": 17,
            "isMjpeg": True,
            "urlEncoded": "",
            "proxyUrl": "",
            "janusGatewayUrl": "",
            "janusToken": "",
            "iceServers": "",
        }
    },
    "included": [],
}


@pytest.mark.asyncio
async def test_unavailable_camera_returns_no_config(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No config, and the warning names why rather than just listing keys."""
    session = _session_returning(UNAVAILABLE_BODY)

    with caplog.at_level(logging.WARNING):
        config = await AlarmCameraSession.get_stream_info(session, "94927580-2053")

    assert config is None

    warning = caplog.text
    assert "errorEnum=17" in warning
    assert "isMjpeg=True" in warning
    assert "janusGatewayUrl=empty" in warning


@pytest.mark.asyncio
async def test_the_janus_token_is_never_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A populated token must not reach the log.

    The warning reports whether Alarm.com filled the fields in, not their
    values - the token is a live credential.
    """
    body = {
        "data": {
            "attributes": {
                **UNAVAILABLE_BODY["data"]["attributes"],
                "janusToken": "super-secret-live-token",
                # Still no config: the gateway URL is what the branch needs.
                "janusGatewayUrl": "",
            }
        },
        "included": [],
    }
    session = _session_returning(body)

    with caplog.at_level(logging.WARNING):
        await AlarmCameraSession.get_stream_info(session, "94927580-2053")

    assert "super-secret-live-token" not in caplog.text
    assert "janusToken=set" in caplog.text

"""
Replacing the websocket connection before Alarm.com expires its token.

Alarm.com closes the connection with code 1008 ("rejected token") about five
minutes after issuing it. Recovering took up to 2m38s on the system this was
measured on, because the first reconnect attempts hang and time out - leaving
the integration blind for roughly a third of its life. Events that arrive in
those gaps are lost outright: a websocket has no replay, and the periodic API
refresh only reports current state, so a door opened and closed during a gap
never happened as far as Home Assistant is concerned.
"""

from __future__ import annotations

# isort: skip_file
# Import order matters here and must not be sorted: importing the component
# runs its sys.path shim, which puts the vendored library on sys.path as
# top-level `_pyalarmdotcomajax`.
import custom_components.alarmdotcom  # noqa: F401

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

import _pyalarmdotcomajax.websocket.client as ws_client
from _pyalarmdotcomajax.websocket.client import WebSocketClient, WebSocketState


class _StopReader(Exception):
    """Break out of the reader's infinite loop once the test has seen enough."""


def _client_with_fake_socket(connects: list[str], connect_limit: int) -> WebSocketClient:
    """Build a client whose websocket connects are recorded and never real."""
    client = WebSocketClient(MagicMock())
    client._authenticate = AsyncMock(return_value=None)

    @contextlib.asynccontextmanager
    async def fake_ws_connect(url: str, **_kwargs: object):
        connects.append(url)

        if len(connects) > connect_limit:
            raise _StopReader

        socket = MagicMock()
        socket.close_code = None
        yield socket

    client._bridge.ws_connect = fake_ws_connect

    return client


@pytest.mark.asyncio
async def test_connection_is_replaced_before_its_token_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reader drops and rebuilds the connection on its own deadline."""
    monkeypatch.setattr(ws_client, "WS_TOKEN_LIFETIME_S", 0.01)

    connects: list[str] = []
    client = _client_with_fake_socket(connects, connect_limit=3)

    async def never_returns(_socket: object) -> None:
        await asyncio.sleep(3600)

    client._read_messages = never_returns

    with pytest.raises(_StopReader):
        await client._event_reader()

    # Three deliberate replacements, not one connection plus failures.
    assert len(connects) == 4


@pytest.mark.asyncio
async def test_a_planned_replacement_is_not_a_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Subscribers must not see the refresh.

    A DISCONNECTED/RECONNECTED pair would put every controller through a full
    reconnect refresh every few minutes, and would tell Home Assistant the
    connection had dropped when it had not.
    """
    monkeypatch.setattr(ws_client, "WS_TOKEN_LIFETIME_S", 0.01)

    connects: list[str] = []
    client = _client_with_fake_socket(connects, connect_limit=2)

    emitted: list[WebSocketState] = []
    client._emit_ws_state = lambda state, next_attempt_s=None: emitted.append(state)

    async def never_returns(_socket: object) -> None:
        await asyncio.sleep(3600)

    client._read_messages = never_returns

    with pytest.raises(_StopReader):
        await client._event_reader()

    assert WebSocketState.DISCONNECTED not in emitted
    assert WebSocketState.WAITING not in emitted
    # Nor a reconnect: that is what drives controllers into a full refresh.
    assert WebSocketState.RECONNECTED not in emitted
    assert emitted == [WebSocketState.CONNECTING, WebSocketState.CONNECTED]


def test_deadline_leaves_margin_under_the_observed_expiry() -> None:
    """
    Measured uptimes were 5m08s at the shortest before the server closed us.

    The deadline has to sit clearly below that, with room for a slow connect.
    """
    shortest_observed_expiry_s = 5 * 60 + 8

    assert shortest_observed_expiry_s - 30 > ws_client.WS_TOKEN_LIFETIME_S


@pytest.mark.asyncio
async def test_read_messages_queues_text_frames_only() -> None:
    """Non-text frames are logged and skipped; text frames reach the processor."""
    client = WebSocketClient(MagicMock())

    def _frame(msg_type: object, data: str) -> MagicMock:
        frame = MagicMock()
        frame.type = msg_type
        frame.data = data
        return frame

    frames = [
        _frame(ws_client.aiohttp.WSMsgType.TEXT, '{"EventType": 100}'),
        _frame(ws_client.aiohttp.WSMsgType.BINARY, "ignored"),
        _frame(ws_client.aiohttp.WSMsgType.TEXT, '{"EventType": 15}'),
    ]

    class _Socket:
        def __aiter__(self):
            async def gen():
                for frame in frames:
                    yield frame

            return gen()

    await client._read_messages(_Socket())

    assert client.last_events == ['{"EventType": 100}', '{"EventType": 15}']
    assert client._event_queue.qsize() == 2

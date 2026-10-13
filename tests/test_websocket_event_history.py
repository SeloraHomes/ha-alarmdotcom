"""
Raw frame retention for diagnostics.

The buffer exists so that "my sensor stopped updating" can be answered by
looking at what actually arrived. At the original cap of 25 frames it could
not: a household with a few phones reporting geofence crossings fills it in
about a minute, so the event under investigation had always aged out before
anyone could download diagnostics.
"""

from __future__ import annotations

# isort: skip_file
# Import order matters here and must not be sorted: importing the component
# runs its sys.path shim, which puts the vendored library on sys.path as
# top-level `_pyalarmdotcomajax`.
import custom_components.alarmdotcom  # noqa: F401

from unittest.mock import MagicMock

from _pyalarmdotcomajax.websocket.client import EVENT_HISTORY_MAXLEN, WebSocketClient


def test_history_holds_enough_frames_to_survive_a_chatty_account() -> None:
    """
    A frame every ~2.5s was measured on a real account; 25 covered 64 seconds.

    The cap has to leave room to notice something, walk to a door, and then
    download diagnostics - minutes, not seconds.
    """
    frames_per_minute = 24

    assert 10 * frames_per_minute <= EVENT_HISTORY_MAXLEN


def test_client_uses_the_cap_for_its_history() -> None:
    """The constant is only meaningful if the deque is actually built from it."""
    client = WebSocketClient(MagicMock())

    assert client._event_history.maxlen == EVENT_HISTORY_MAXLEN


def test_last_events_returns_what_was_recorded() -> None:
    """`last_events` is what diagnostics surfaces; it must copy, not alias."""
    client = WebSocketClient(MagicMock())
    client._event_history.append('{"EventType": 100}')

    events = client.last_events

    assert events == ['{"EventType": 100}']

    events.append("mutating the returned list must not touch the buffer")
    assert len(client.last_events) == 1

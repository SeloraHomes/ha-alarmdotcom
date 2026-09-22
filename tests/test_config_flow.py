"""
Tests for the alarmdotcom config flow.

Covers the Bronze quality-scale "config-flow-test-coverage" requirement:
the initial login step, all three login failure modes shown to the user
(cannot_connect, invalid_auth, unknown), the OTP method-selection step
(including the auto-skip-when-only-app-is-enabled case), OTP submission
(including the invalid-code case), duplicate-entry abort, and the options
flow's two steps.
"""

from unittest.mock import AsyncMock

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.alarmdotcom._pyalarmdotcomajax as pyadc
from custom_components.alarmdotcom.const import (
    CONF_ARM_AWAY,
    CONF_ARM_CODE,
    CONF_ARM_HOME,
    CONF_ARM_NIGHT,
    CONF_CAMERA_TOKEN_REFRESH_INTERVAL,
    CONF_MFA_TOKEN,
    CONF_OPTIONS_DEFAULT,
    CONF_OTP,
    CONF_OTP_METHOD,
    CONF_REMOVE_ARM_CODE,
    DOMAIN,
)

VALID_CREDS = {CONF_USERNAME: "test@example.com", CONF_PASSWORD: "hunter2"}


async def _start_user_flow(hass: HomeAssistant) -> dict:
    """Kick off the config flow and land on the initial user step."""
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_user_step_shows_form(hass: HomeAssistant) -> None:
    """The first thing a user sees is the username/password form."""
    result = await _start_user_flow(hass)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_full_flow_success_no_otp(
    hass: HomeAssistant, mock_bridge_class, mock_bridge, mock_setup_entry
) -> None:
    """A user without 2FA enabled logs in and gets a config entry immediately."""
    result = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], VALID_CREDS
    )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "Test System (test@example.com)"
    assert result["data"][CONF_USERNAME] == VALID_CREDS[CONF_USERNAME]
    assert result["data"][CONF_PASSWORD] == VALID_CREDS[CONF_PASSWORD]

    # Unique ID should be the Alarm.com system ID, not something derived
    # from the username - this is what prevents a user from accidentally
    # adding the same system twice under two different login attempts.
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].unique_id == "12345"


@pytest.mark.parametrize(
    ("login_side_effect", "expected_error"),
    [
        (TimeoutError(), "cannot_connect"),
        (pyadc.UnexpectedResponse("boom"), "cannot_connect"),
        (pyadc.NotAuthorized(), "cannot_connect"),
        (pyadc.AuthenticationFailed(), "invalid_auth"),
        (ValueError("something unrelated broke"), "unknown"),
    ],
)
async def test_login_failure_modes(
    hass: HomeAssistant, mock_bridge_class, mock_bridge, login_side_effect, expected_error
) -> None:
    """
    Every distinct login failure should surface its own specific error to the user.

    This matters because #21 (OTP "Failed to Connect") existed specifically
    because a different failure was being reported as cannot_connect -
    asserting each exception maps to its own error code, not just "some
    error happened", is what would have caught that class of regression.
    """
    mock_bridge.login = AsyncMock(side_effect=login_side_effect)

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], VALID_CREDS
    )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected_error}


async def test_must_configure_mfa_aborts(hass: HomeAssistant, mock_bridge_class, mock_bridge) -> None:
    """If Alarm.com requires 2FA to be enabled account-wide, abort with a clear reason."""
    mock_bridge.login = AsyncMock(side_effect=pyadc.MustConfigureMfa())

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], VALID_CREDS
    )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "must_enable_2fa"


async def test_otp_flow_with_method_selection(
    hass: HomeAssistant, mock_bridge_class, mock_bridge, mock_setup_entry
) -> None:
    """A user with SMS + email 2FA enabled sees a method picker, then an OTP prompt."""
    mock_bridge.login = AsyncMock(
        side_effect=pyadc.OtpRequired(enabled_2fa_methods=[pyadc.OtpType.sms, pyadc.OtpType.email])
    )

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], VALID_CREDS
    )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "otp_select_method"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTP_METHOD: "sms"}
    )

    mock_bridge.auth_controller.request_otp.assert_awaited_once_with(pyadc.OtpType.sms)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "otp_submit"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTP: "123456"}
    )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    mock_bridge.auth_controller.submit_otp.assert_awaited_once()


async def test_otp_flow_auto_skips_method_selection_for_app_only(
    hass: HomeAssistant, mock_bridge_class, mock_bridge
) -> None:
    """
    If authenticator-app is the ONLY enabled method, skip straight to the OTP prompt.

    There's nothing to request for the app method (no SMS/email to trigger),
    so making the user pick from a list of one option is pure friction.
    """
    mock_bridge.login = AsyncMock(
        side_effect=pyadc.OtpRequired(enabled_2fa_methods=[pyadc.OtpType.app])
    )

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], VALID_CREDS
    )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "otp_submit"
    mock_bridge.auth_controller.request_otp.assert_not_awaited()


async def test_otp_method_selection_shows_masked_destinations(
    hass: HomeAssistant, mock_bridge_class, mock_bridge
) -> None:
    """
    The method picker names where each code would actually go.

    Without this the picker just said "Text Message", so a number Alarm.com
    can't deliver to (a decommissioned line, or a VoIP number that silently
    drops SMS) was indistinguishable from a broken integration.
    """
    mock_bridge.login = AsyncMock(
        side_effect=pyadc.OtpRequired(
            enabled_2fa_methods=[pyadc.OtpType.sms, pyadc.OtpType.email],
            email="test@example.com",
            sms_number="5555550123",
            sms_country_code="1",
        )
    )

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], VALID_CREDS
    )

    assert result["step_id"] == "otp_select_method"
    destinations = result["description_placeholders"]["destinations"]

    assert "0123" in destinations
    assert "t•••@example.com" in destinations
    # Only the last four digits survive masking.
    assert sum(char.isdigit() for char in destinations) == 4


async def test_otp_submit_names_the_sms_destination(
    hass: HomeAssistant, mock_bridge_class, mock_bridge
) -> None:
    """The code-entry screen says which number the text was sent to."""
    mock_bridge.login = AsyncMock(
        side_effect=pyadc.OtpRequired(
            enabled_2fa_methods=[pyadc.OtpType.sms],
            sms_number="5555550123",
            sms_country_code="1",
        )
    )

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], VALID_CREDS
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTP_METHOD: "sms"}
    )

    assert result["step_id"] == "otp_submit"
    destination = result["description_placeholders"]["destination"]
    assert destination.startswith("sent by text message to ")
    assert destination.endswith("0123")


async def test_otp_submit_does_not_claim_a_code_was_sent_for_app_only(
    hass: HomeAssistant, mock_bridge_class, mock_bridge
) -> None:
    """
    Nothing is sent for the authenticator-app method, so don't say it was.

    The old wording ("the one-time code sent to your chosen device") had users
    waiting on a text message that was never requested.
    """
    mock_bridge.login = AsyncMock(
        side_effect=pyadc.OtpRequired(enabled_2fa_methods=[pyadc.OtpType.app])
    )

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], VALID_CREDS
    )

    assert result["step_id"] == "otp_submit"
    assert (
        result["description_placeholders"]["destination"]
        == "from your authenticator app"
    )


async def test_reauth_reuses_stored_mfa_token(
    hass: HomeAssistant, mock_bridge_class, mock_bridge, mock_setup_entry
) -> None:
    """
    Reauth logs back in with the entry's device-trust token.

    That token is what tells Alarm.com the device was already verified.
    Dropping it made every reauth look like a brand-new device and forced a
    fresh OTP - and with SMS, a fresh text that may never arrive.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="12345",
        data={**VALID_CREDS, CONF_MFA_TOKEN: "stored-token"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], VALID_CREDS
    )

    assert mock_bridge_class.call_args.kwargs["mfa_token"] == "stored-token"
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_MFA_TOKEN] == "stored-token"


async def test_reauth_discards_a_rejected_mfa_token(
    hass: HomeAssistant, mock_bridge_class, mock_bridge, mock_setup_entry
) -> None:
    """
    A carried-over token that still needs an OTP is dropped, not saved back.

    submit_otp() decides the OTP succeeded by checking that the controller
    holds any MFA cookie at all, so a dead token left in place would pass that
    check and be written straight back to the entry - leaving the user in a
    loop of reauths that each demand a fresh code.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="12345",
        data={**VALID_CREDS, CONF_MFA_TOKEN: "stale-token"},
    )
    entry.add_to_hass(hass)

    mock_bridge.auth_controller.mfa_cookie = "stale-token"
    mock_bridge.login = AsyncMock(
        side_effect=pyadc.OtpRequired(enabled_2fa_methods=[pyadc.OtpType.app])
    )
    mock_bridge.auth_controller.submit_otp = AsyncMock(return_value="fresh-token")

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], VALID_CREDS
    )

    assert result["step_id"] == "otp_submit"
    assert mock_bridge.auth_controller.mfa_cookie == ""

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTP: "123456"}
    )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert entry.data[CONF_MFA_TOKEN] == "fresh-token"


async def test_invalid_otp_code_shows_error(
    hass: HomeAssistant, mock_bridge_class, mock_bridge
) -> None:
    """A wrong OTP code re-shows the form with an error, not a crash."""
    mock_bridge.login = AsyncMock(
        side_effect=pyadc.OtpRequired(enabled_2fa_methods=[pyadc.OtpType.app])
    )
    mock_bridge.auth_controller.submit_otp = AsyncMock(return_value=None)

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], VALID_CREDS
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTP: "000000"}
    )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "otp_submit"
    assert result["errors"] == {"base": "invalid_otp"}


async def test_duplicate_system_aborts(hass: HomeAssistant, mock_bridge_class, mock_bridge) -> None:
    """Logging into a system that's already configured should abort, not duplicate."""
    MockConfigEntry(domain=DOMAIN, unique_id="12345", data=VALID_CREDS).add_to_hass(hass)

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], VALID_CREDS
    )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_full_walkthrough(hass: HomeAssistant) -> None:
    """The options flow's three steps (arm code, arm mode profiles, then polling intervals) all work."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="12345", data=VALID_CREDS)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_ARM_CODE: "1234", CONF_REMOVE_ARM_CODE: False},
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "modes"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_ARM_HOME: ["silent_arming"], CONF_ARM_AWAY: [], CONF_ARM_NIGHT: []},
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "polling"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"activity_poll_interval": 15, "full_state_poll_interval": 5},
    )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ARM_CODE] == "1234"
    assert result["data"][CONF_ARM_HOME] == ["silent_arming"]
    assert result["data"]["activity_poll_interval"] == 15
    assert result["data"]["full_state_poll_interval"] == 5


async def test_options_flow_polling_step_accepts_custom_intervals(hass: HomeAssistant) -> None:
    """The polling step actually persists custom (non-default) interval values, not just the defaults."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="12345", data=VALID_CREDS)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ARM_CODE: "", CONF_REMOVE_ARM_CODE: False}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ARM_HOME: [], CONF_ARM_AWAY: [], CONF_ARM_NIGHT: []}
    )
    assert result["step_id"] == "polling"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"activity_poll_interval": 60, "full_state_poll_interval": 15},
    )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"]["activity_poll_interval"] == 60
    assert result["data"]["full_state_poll_interval"] == 15


async def test_options_flow_polling_step_persists_camera_token_refresh(hass: HomeAssistant) -> None:
    """The camera token refresh interval (#93) is set on the polling step and persisted."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="12345", data=VALID_CREDS)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"arm_code": "", "remove_arm_code": False}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"arm_home_options": [], "arm_away_options": [], "arm_night_options": []},
    )
    assert result["step_id"] == "polling"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "activity_poll_interval": 15,
            "full_state_poll_interval": 5,
            "camera_token_refresh_interval": 10,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"]["camera_token_refresh_interval"] == 10


def test_camera_token_refresh_default_is_the_previously_hardcoded_thirty_minutes() -> None:
    """Existing installs must keep the 30-minute cadence they had before this was an option."""
    assert CONF_OPTIONS_DEFAULT[CONF_CAMERA_TOKEN_REFRESH_INTERVAL] == 30

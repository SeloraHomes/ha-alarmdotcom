"""Config flow to configure Alarmdotcom."""

import asyncio
import logging
from typing import Any, Literal

import _pyalarmdotcomajax as pyadc
import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_ACTIVITY_POLL_INTERVAL,
    CONF_ARM_AWAY,
    CONF_ARM_CODE,
    CONF_ARM_HOME,
    CONF_ARM_MODE_OPTIONS,
    CONF_ARM_NIGHT,
    CONF_CAMERA_TOKEN_REFRESH_INTERVAL,
    CONF_FULL_STATE_POLL_INTERVAL,
    CONF_MFA_TOKEN,
    CONF_OPTIONS_DEFAULT,
    CONF_OTP,
    CONF_OTP_METHOD,
    CONF_OTP_METHODS_LIST,
    CONF_REMOVE_ARM_CODE,
    DOMAIN,
)

LOGGER = logging.getLogger(__name__)
LegacyArmingOptions = Literal["home", "away", "true", "false"]


MASK_CHAR = "•"


def _mask_phone_number(number: str) -> str:
    """
    Mask all but the last four digits of a phone number.

    Enough for the user to recognize which of their numbers Alarm.com holds
    on file - the point of showing it at all - without reprinting the whole
    number on screen. Non-digits (formatting) are preserved so the number
    keeps its familiar shape.
    """
    chars = list(number)
    digit_positions = [i for i, char in enumerate(chars) if char.isdigit()]

    for position in digit_positions[:-4]:
        chars[position] = MASK_CHAR

    return "".join(chars)


def _mask_email(email: str) -> str:
    """
    Mask the local part of an email address, keeping the domain readable.

    A value without an "@" is not an address we recognize, so it is masked
    whole rather than printed verbatim.
    """
    local, separator, domain = email.partition("@")
    masked_local = local[:1] + MASK_CHAR * max(len(local) - 1, 1)

    return f"{masked_local}@{domain}" if separator else masked_local


class ADCFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle an Alarmdotcom config flow."""

    VERSION = 4

    def __init__(self) -> None:
        """Initialize the Alarmdotcom flow."""
        self.config: dict[str, Any] = {}
        self.system_id: str | None = None
        self.sensor_data: dict[str, Any] | None = None
        self._config_title: str | None = None
        self.bridge: pyadc.AlarmBridge | None = None
        self._existing_entry: config_entries.ConfigEntry | None = None
        self._otp_options: pyadc.OtpRequired | None = None
        self.otp_method: pyadc.OtpType | None = None

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "ADCOptionsFlowHandler":
        """Tell Home Assistant that this integration supports configuration options."""
        return ADCOptionsFlowHandler(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Gather configuration data when flow is initiated via the user interface."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Reuse the device-trust token from the entry being reauthenticated.
            # It is what tells Alarm.com this device was already verified, so
            # carrying it over means an expired session is repaired with just a
            # password - without it every reauth looked like a brand-new device
            # and forced a fresh OTP (and, with SMS, a fresh text message that
            # may never arrive). A stale or rejected token costs nothing: the
            # login simply reports the device as untrusted and we fall through
            # to the normal OTP steps below.
            self.config = {
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_MFA_TOKEN: user_input.get(CONF_MFA_TOKEN)
                or (
                    self._existing_entry.data.get(CONF_MFA_TOKEN)
                    if self._existing_entry
                    else None
                ),
            }

            LOGGER.debug("Logging in to Alarm.com...")

            self.bridge = pyadc.AlarmBridge(
                username=self.config[CONF_USERNAME],
                password=self.config[CONF_PASSWORD],
                mfa_token=self.config[CONF_MFA_TOKEN],
            )

            async with asyncio.timeout(60):
                try:
                    await self.bridge.login()
                except pyadc.OtpRequired as exc:
                    LOGGER.debug("OTP code required. Moving to selection step.")
                    # Reaching this step proves the carried-over token did not
                    # get us in, so drop it. submit_otp() only checks that *a*
                    # cookie exists when deciding the OTP succeeded, so leaving
                    # the dead one in place would let it pass that check and
                    # save the same dead token straight back to the entry.
                    self.config[CONF_MFA_TOKEN] = None
                    self.bridge.auth_controller.mfa_cookie = ""
                    self._otp_options = exc
                    return await self.async_step_otp_select_method()
                except pyadc.MustConfigureMfa:
                    return self.async_abort(reason="must_enable_2fa")
                except (
                    TimeoutError,
                    aiohttp.ClientError,
                    pyadc.UnexpectedResponse,
                    pyadc.NotAuthorized,
                ):
                    LOGGER.exception("User login failed to contact Alarm.com.")
                    errors["base"] = "cannot_connect"
                except pyadc.AuthenticationFailed:
                    errors["base"] = "invalid_auth"
                except Exception:
                    LOGGER.exception("Got error while initializing Alarm.com.")
                    errors["base"] = "unknown"
                else:
                    return await self.async_step_final()

        creds_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.TEXT,
                        autocomplete="username",
                    )
                ),
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.PASSWORD,
                        autocomplete="current-password",
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=creds_schema,
            errors=errors,
            last_step=False,
        )

    def _otp_destination_value(self, method: pyadc.OtpType | None) -> str | None:
        """
        Return the masked phone number or email address Alarm.com holds for this method.

        Alarm.com reports both on the OtpRequired exception, but the user never
        saw either, which made an undeliverable destination - a decommissioned
        number, or a VoIP line that silently drops SMS - look like a broken
        integration rather than an account setting to fix.
        """
        if not self._otp_options:
            return None

        if method == pyadc.OtpType.sms:
            number = (
                self._otp_options.formatted_sms_number or self._otp_options.sms_number
            )
            return _mask_phone_number(number) if number else None

        if method == pyadc.OtpType.email:
            return _mask_email(self._otp_options.email) if self._otp_options.email else None

        return None

    def _otp_destination(self, method: pyadc.OtpType | None) -> str:
        """Complete the code-entry screen's "Enter the one-time code {destination}."."""
        destination = self._otp_destination_value(method)

        if method == pyadc.OtpType.app:
            return "from your authenticator app"

        if method == pyadc.OtpType.sms:
            return (
                f"sent by text message to {destination}"
                if destination
                else "sent by text message"
            )

        if method == pyadc.OtpType.email:
            return f"sent by email to {destination}" if destination else "sent by email"

        return "sent to you by Alarm.com"

    def _otp_destination_summary(self, methods: list[pyadc.OtpType]) -> str:
        """List each offered delivery method alongside where its code would go."""
        labels = {
            pyadc.OtpType.app: "Authenticator app",
            pyadc.OtpType.sms: "Text message",
            pyadc.OtpType.email: "Email",
        }

        lines = []
        for method in methods:
            label = labels.get(method, method.name)

            if method == pyadc.OtpType.app:
                lines.append(f"{label}: code generated in the app")
                continue

            lines.append(
                f"{label}: "
                f"{self._otp_destination_value(method) or 'destination not reported by Alarm.com'}"
            )

        return "\n".join(lines)

    async def async_step_otp_select_method(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select OTP method when integration configured through UI."""
        # self.bridge is always set by this point: this step is only ever
        # reached via async_step_user's own successful bridge creation, never
        # invoked independently.
        assert self.bridge is not None
        if not self._otp_options:
            return self.async_abort(reason="no_otp_options")

        enabled_methods = self._otp_options.enabled_2fa_methods
        if not enabled_methods:
            return self.async_abort(reason="no_otp_options")

        errors: dict[str, str] = {}

        # Sensible auto skip:
        # if authenticator app is the ONLY method, nothing needs to be sent.
        # For SMS or email, always show the selector so request_otp() is triggered.
        if (
            user_input is None
            and len(enabled_methods) == 1
            and enabled_methods[0] == pyadc.OtpType.app
        ):
            self.otp_method = enabled_methods[0]
            LOGGER.debug(
                "Only authenticator app is enabled. Skipping method selection."
            )
            return await self.async_step_otp_submit()

        if user_input is not None:
            selected_name = user_input[CONF_OTP_METHOD]
            self.otp_method = getattr(pyadc.OtpType, selected_name.lower(), None)

            if not self.otp_method:
                errors["base"] = "invalid_otp_method"
            else:
                try:
                    if self.otp_method in (pyadc.OtpType.email, pyadc.OtpType.sms):
                        LOGGER.debug(
                            "Requesting OTP via %s...", self.otp_method.name
                        )
                        await self.bridge.auth_controller.request_otp(self.otp_method)

                    return await self.async_step_otp_submit()

                except (
                    TimeoutError,
                    aiohttp.ClientError,
                    pyadc.UnexpectedResponse,
                    pyadc.NotAuthorized,
                ):
                    LOGGER.exception("Failed to request OTP.")
                    errors["base"] = "cannot_connect"
                except Exception:
                    LOGGER.exception("Unexpected error while requesting OTP.")
                    errors["base"] = "unknown"

        otp_method_schema = vol.Schema(
            {
                vol.Required(
                    CONF_OTP_METHOD,
                    default=enabled_methods[0].name if enabled_methods else None,
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[m.name for m in enabled_methods],
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_OTP_METHODS_LIST,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="otp_select_method",
            data_schema=otp_method_schema,
            errors=errors,
            description_placeholders={
                "destinations": self._otp_destination_summary(enabled_methods)
            },
            last_step=False,
        )

    async def async_step_otp_submit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Gather OTP when integration configured through UI."""
        # self.bridge is always set by this point - see async_step_otp_select_method.
        assert self.bridge is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            if not self.otp_method:
                return self.async_abort(reason="otp_method_lost")

            try:
                mfa_cookie = await self.bridge.auth_controller.submit_otp(
                    method=self.otp_method,
                    code=user_input[CONF_OTP],
                    device_name=f"Home Assistant {self.hass.config.location_name}",
                )

                if mfa_cookie:
                    self.config[CONF_MFA_TOKEN] = mfa_cookie
                    return await self.async_step_final()

                errors["base"] = "invalid_otp"

            except pyadc.AuthenticationFailed:
                errors["base"] = "invalid_otp"
            except (
                TimeoutError,
                aiohttp.ClientError,
                pyadc.UnexpectedResponse,
                pyadc.NotAuthorized,
            ):
                LOGGER.exception("OTP submission failed.")
                errors["base"] = "cannot_connect"
            except Exception:
                LOGGER.exception("Unexpected error during OTP submission.")
                errors["base"] = "unknown"

        creds_schema = vol.Schema(
            {
                vol.Required(CONF_OTP): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.TEXT,
                        autocomplete="one-time-code",
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="otp_submit",
            data_schema=creds_schema,
            errors=errors,
            description_placeholders={
                "destination": self._otp_destination(self.otp_method)
            },
            last_step=True,
        )

    async def async_step_final(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create configuration entry using entered data."""
        # self.bridge is always set by this point - see async_step_otp_select_method.
        assert self.bridge is not None
        await self.bridge.fetch_full_state()

        system_id = str(self.bridge.active_system.id)
        await self.async_set_unique_id(system_id)

        self._config_title = (
            f"{self.bridge.active_system.name} "
            f"({self.bridge.auth_controller.user_email})"
        )

        if self._existing_entry:
            # Deliberately async_update_and_abort, not the older, separate
            # async_update_entry() + async_reload() pair this used to be:
            # combining an explicit reload with the config-entry update
            # listener already registered in hub.py (which itself reloads
            # in response to ANY entry update, not just options) is exactly
            # the double-reload/race-condition pattern Home Assistant
            # deprecated in 2026.6 (hard error from 2026.12). Verified
            # directly against home-assistant/core's 2026.7.1 source:
            # async_update_and_abort has no reload of its own - it updates
            # the entry (firing the listener, which does the one necessary
            # reload) and aborts, with reason correctly defaulting to
            # "reauth_successful" for this flow source.
            return self.async_update_and_abort(
                self._existing_entry,
                data=self.config,
            )

        # Only enforced for a brand-new entry, not reauth: during reauth the
        # unique_id is expected to match the entry currently being
        # reauthenticated (handled above), so aborting here would incorrectly
        # block a legitimate reauth.
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=self._config_title,
            data=self.config,
            options=CONF_OPTIONS_DEFAULT,
        )

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Perform reauth upon an API authentication error."""
        self._existing_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm(user_input)

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Dialog that informs the user that reauth is required."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({}),
            )
        return await self.async_step_user()


class ADCOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle option configuration via Integrations page."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.options = dict(config_entry.options)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """First screen for configuration options. Sets arming code."""
        if user_input is not None:
            if user_input[CONF_REMOVE_ARM_CODE]:
                user_input[CONF_ARM_CODE] = ""
            self.options.update(user_input)
            self.options.pop(CONF_REMOVE_ARM_CODE, None)
            return await self.async_step_modes()

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_ARM_CODE,
                    default=self.options.get(CONF_ARM_CODE, ""),
                ): selector.selector({"text": {"type": "password"}}),
                vol.Optional(
                    CONF_REMOVE_ARM_CODE,
                    default=False,
                ): selector.selector({"boolean": {}}),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_modes(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set arming mode profiles."""
        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_polling()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ARM_HOME,
                    default=self.options.get(
                        CONF_ARM_HOME,
                        CONF_OPTIONS_DEFAULT[CONF_ARM_HOME],
                    ),
                ): cv.multi_select(CONF_ARM_MODE_OPTIONS),
                vol.Required(
                    CONF_ARM_AWAY,
                    default=self.options.get(
                        CONF_ARM_AWAY,
                        CONF_OPTIONS_DEFAULT[CONF_ARM_AWAY],
                    ),
                ): cv.multi_select(CONF_ARM_MODE_OPTIONS),
                vol.Required(
                    CONF_ARM_NIGHT,
                    default=self.options.get(
                        CONF_ARM_NIGHT,
                        CONF_OPTIONS_DEFAULT[CONF_ARM_NIGHT],
                    ),
                ): cv.multi_select(CONF_ARM_MODE_OPTIONS),
            }
        )

        return self.async_show_form(
            step_id="modes",
            data_schema=schema,
        )

    async def async_step_polling(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Configure how often this integration polls Alarm.com.

        Both intervals are user-configurable rather than fixed constants,
        specifically so either can be dialed back by anyone who runs into
        a real problem, without needing a code change. The activity poll
        (lock unlock attribution, the general activity feed) hits an
        entirely undocumented Alarm.com endpoint with no confirmed rate
        limit - the default of 15 seconds is a deliberate tradeoff, not a
        guarantee it's always safe to leave that low.
        """
        if user_input is not None:
            self.options.update(user_input)
            return self.async_create_entry(title="", data=self.options)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ACTIVITY_POLL_INTERVAL,
                    default=self.options.get(
                        CONF_ACTIVITY_POLL_INTERVAL,
                        CONF_OPTIONS_DEFAULT[CONF_ACTIVITY_POLL_INTERVAL],
                    ),
                ): selector.selector(
                    {"number": {"min": 15, "max": 300, "step": 1, "unit_of_measurement": "seconds"}}
                ),
                vol.Required(
                    CONF_FULL_STATE_POLL_INTERVAL,
                    default=self.options.get(
                        CONF_FULL_STATE_POLL_INTERVAL,
                        CONF_OPTIONS_DEFAULT[CONF_FULL_STATE_POLL_INTERVAL],
                    ),
                ): selector.selector(
                    {"number": {"min": 1, "max": 60, "step": 1, "unit_of_measurement": "minutes"}}
                ),
                vol.Required(
                    CONF_CAMERA_TOKEN_REFRESH_INTERVAL,
                    default=self.options.get(
                        CONF_CAMERA_TOKEN_REFRESH_INTERVAL,
                        CONF_OPTIONS_DEFAULT[CONF_CAMERA_TOKEN_REFRESH_INTERVAL],
                    ),
                ): selector.selector(
                    {"number": {"min": 5, "max": 120, "step": 1, "unit_of_measurement": "minutes"}}
                ),
            }
        )

        return self.async_show_form(
            step_id="polling",
            data_schema=schema,
            last_step=True,
        )

"""Utility functions."""

from typing import TYPE_CHECKING

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


def slug_to_title(slug: str) -> str:
    """Convert slug to title case."""

    return slug.replace("_", " ").title()


async def cleanup_orphaned_entities_and_devices(
    hass: "HomeAssistant",
    config_entry: "ConfigEntry",
    current_entity_ids: set[str],
    current_unique_ids: set[str],
    platform: str,
) -> None:
    """
    Remove orphaned entities and devices for a given platform.

    Args:
        hass: HomeAssistant instance
        config_entry: The config entry for the integration
        current_entity_ids: Set of entity_ids currently provided by the integration
        current_unique_ids: Set of unique_ids currently provided by the integration
        platform: The Home Assistant platform (e.g., "light", "lock", "cover", etc.)

    """
    entity_registry = async_get_entity_registry(hass)

    # Remove orphaned entities for this platform.
    # An entity is orphaned when NEITHER its entity_id NOR its unique_id is
    # present in the current set. Entities without a unique_id are matched
    # solely by entity_id — we must not delete them just because unique_id is absent.
    for entry in list(entity_registry.entities.values()):
        if entry.config_entry_id != config_entry.entry_id or entry.domain != platform:
            continue

        matched_by_entity_id = entry.entity_id in current_entity_ids
        matched_by_unique_id = bool(entry.unique_id) and entry.unique_id in current_unique_ids

        if not matched_by_entity_id and not matched_by_unique_id:
            entity_registry.async_remove(entry.entity_id)

    # Device removal must not run from individual platform setup. During a
    # reload, other platforms may not have registered their entities yet.
    # cleanup_orphaned_devices runs once all platforms are set up.


def _owned_by_other_entry(device: dr.DeviceEntry, entry_id: str) -> bool:
    """Return True if a config entry other than ``entry_id`` also owns the device."""
    # Newer Home Assistant gives each device one config entry and deprecates
    # DeviceEntry.config_entries; older releases (still supported) only have that set.
    if hasattr(device, "config_entry_id"):
        return device.config_entry_id != entry_id
    return device.config_entries != {entry_id}


def cleanup_orphaned_devices(
    hass: "HomeAssistant",
    config_entry: "ConfigEntry",
    current_resource_ids: set[str],
) -> None:
    """
    Remove devices this entry owns that no longer have any entity.

    Run once, after every platform has been set up: per-platform setup cannot
    do it, because during a reload the other platforms have not re-registered
    their entities yet. The runtime RESOURCE_DELETED handler only sees
    deletions while the integration is running; this catches devices removed
    at Alarm.com while Home Assistant was down.

    A device is kept if Alarm.com still lists it, if it is a service device,
    if another integration shares it, or if another device names it as parent.
    """
    device_registry = dr.async_get(hass)
    entity_registry = async_get_entity_registry(hass)
    devices = dr.async_entries_for_config_entry(device_registry, config_entry.entry_id)
    parent_ids = {device.via_device_id for device in devices if device.via_device_id}

    for device in devices:
        if (
            _owned_by_other_entry(device, config_entry.entry_id)
            or device.entry_type == dr.DeviceEntryType.SERVICE
            or device.id in parent_ids
            or any(domain == DOMAIN and ident in current_resource_ids for domain, ident in device.identifiers)
        ):
            continue
        if er.async_entries_for_device(entity_registry, device.id, include_disabled_entities=True):
            continue
        device_registry.async_remove_device(device.id)

"""Base entity helpers for Deepal."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DeepalDataUpdateCoordinator


class DeepalEntity(CoordinatorEntity[DeepalDataUpdateCoordinator]):
    """Base Deepal entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DeepalDataUpdateCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.vehicle_id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        vehicle = self.coordinator.data.get("vehicle", {}) if self.coordinator.data else {}
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.vehicle_id)},
            manufacturer="Changan Deepal",
            model=vehicle.get("modelName") or vehicle.get("modelCode"),
            name=vehicle.get("carName") or vehicle.get("vin") or f"Deepal {self.coordinator.vehicle_id}",
        )

    @property
    def condition(self) -> dict[str, Any]:
        return self.coordinator.data.get("condition", {}) if self.coordinator.data else {}

    async def async_poll_command_update(
        self,
        command_id: str,
        *,
        previous_last_updated: str | None = None,
        is_done: Callable[[], bool] | None = None,
        timeout: float = 30,
        interval: float = 2,
    ) -> None:
        """Poll command result and refresh vehicle data after a command."""
        await self.coordinator.async_poll_command_update(
            command_id,
            previous_last_updated=previous_last_updated or self.coordinator._condition_last_updated(),
            is_done=is_done,
            timeout=timeout,
            interval=interval,
        )

    async def async_execute_command(
        self,
        send_command: Callable[[], Awaitable[str]],
        *,
        is_done: Callable[[], bool] | None = None,
        timeout: float = 30,
        interval: float = 2,
    ) -> None:
        """Send one command and wait until vehicle condition data updates."""
        await self.coordinator.async_execute_command(
            send_command,
            is_done=is_done,
            timeout=timeout,
            interval=interval,
        )

    def raise_command_reauth_required(self, err: Exception) -> None:
        """Create a repair issue for command key failures, then raise for the service call."""
        ir.async_create_issue(
            self.coordinator.hass,
            DOMAIN,
            f"{self.coordinator.entry.entry_id}_command_reauth",
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key="command_reauth",
        )
        raise HomeAssistantError(
            "Deepal remote commands need reauthentication. Open the Deepal repair and complete SMS login again."
        ) from err

"""Base entity helpers for Deepal."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
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
        is_done: Callable[[], bool] | None = None,
        timeout: float = 12,
        interval: float = 2,
    ) -> None:
        """Poll command result and refresh vehicle data after a command."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            result: dict[str, Any] = {}
            try:
                result = await self.coordinator.client.control_result(
                    vehicle_id=self.coordinator.vehicle_id,
                    command_id=command_id,
                )
            finally:
                await self.coordinator.async_request_refresh()

            if is_done is not None and is_done():
                return

            result_code = result.get("resultCode")
            if result_code not in (None, -100):
                if result_code in (0, 1015):
                    return
                raise HomeAssistantError(f"Deepal command failed with result code {result_code}: {result.get('errorMsg')}")

            if loop.time() >= deadline:
                return

            await asyncio.sleep(interval)

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

"""GPS device tracker for a Deepal vehicle."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import _as_int
from .coordinator import DeepalDataUpdateCoordinator
from .entity import DeepalEntity


def _finite_float(value: Any) -> float | None:
    """Parse a finite float, rejecting blank, non-numeric, and non-finite values."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _gps_coordinates(condition: dict[str, Any]) -> tuple[float | None, float | None]:
    """Return lat/lng when locationGeo is a live fix.

    Return ``(None, None)`` only when location is missing, switched off, still on
    the ``2000-`` sentinel timestamp, not a finite coordinate pair, or both
    coordinates are exactly 0. A reported longitude of 0.0 with a real latitude
    is left as-is: the cloud lookup sometimes returns that and it is not a
    missing fix.
    """
    loc = condition.get("locationGeo") if isinstance(condition, dict) else None
    if not isinstance(loc, dict):
        return None, None
    if _as_int(loc.get("locationSwitch")) == 0:
        return None, None
    gps_time = loc.get("gpsTime")
    gps_text = "" if gps_time is None else str(gps_time).strip()
    if not gps_text or gps_text.startswith("2000-"):
        return None, None
    lat = _finite_float(loc.get("lat"))
    lng = _finite_float(loc.get("lng"))
    if lat is None or lng is None:
        return None, None
    if lat == 0.0 and lng == 0.0:
        return None, None
    return lat, lng


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: DeepalDataUpdateCoordinator = entry.runtime_data
    async_add_entities([DeepalLocationTracker(coordinator)])


class DeepalLocationTracker(DeepalEntity, TrackerEntity):
    """GPS tracker sourced from ``condition['locationGeo']``."""

    _attr_translation_key = "location"
    _attr_name = "Location"
    _attr_icon = "mdi:car"
    _attr_entity_category = None
    _attr_source_type = SourceType.GPS

    def __init__(self, coordinator: DeepalDataUpdateCoordinator) -> None:
        super().__init__(coordinator, "location")

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        return _gps_coordinates(self.condition)[0]

    @property
    def longitude(self) -> float | None:
        return _gps_coordinates(self.condition)[1]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        loc = self.condition.get("locationGeo")
        if not isinstance(loc, dict):
            loc = {}
        gps_time = loc.get("gpsTime")
        return {
            "altitude": _finite_float(loc.get("alti")),
            "heading": _finite_float(loc.get("heading")),
            "satellites": _as_int(loc.get("satNum")),
            "gps_time": None if gps_time is None else str(gps_time),
            "location_switch": _as_int(loc.get("locationSwitch")) == 1,
        }

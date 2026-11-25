"""Vehicle location device tracker and S05 locationGeo mapping."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[1])
if sys.path[0] != _ROOT:
    sys.path.insert(0, _ROOT)

from homeassistant.components.device_tracker import SourceType
from homeassistant.const import Platform

from custom_components.deepal.api import _s05_condition_from_params
from custom_components.deepal.const import PLATFORMS
from custom_components.deepal.device_tracker import DeepalLocationTracker

_LIVE = {
    "hashCode": 123456789,
    "lng": "0.0",
    "lat": "51.5007",
    "alti": "15.0",
    "gpsTime": "2026-01-01 12:00:00",
    "satNum": "8",
    "heading": "0.0",
    "locationSwitch": 1,
}
_OFF = {
    "lat": "0.0",
    "lng": "0.0",
    "alti": "0.0",
    "heading": "0.0",
    "satNum": "0",
    "gpsTime": "2000-01-01 00:00:00",
    "locationSwitch": 0,
}


class _Coordinator:
    def __init__(self, condition: dict) -> None:
        self.vehicle_id = "veh-1"
        self.data = {"condition": condition, "vehicle": {"carName": "Deepal"}}


def _tracker(location_geo) -> DeepalLocationTracker:
    condition = {} if location_geo is None else {"locationGeo": location_geo}
    return DeepalLocationTracker(_Coordinator(condition))


class LocationTrackerTests(unittest.TestCase):
    def test_platform_is_registered(self) -> None:
        self.assertIn(Platform.DEVICE_TRACKER, PLATFORMS)

    def test_entity_metadata(self) -> None:
        tracker = _tracker(_LIVE)
        self.assertEqual(tracker.source_type, SourceType.GPS)
        self.assertEqual(tracker.translation_key, "location")
        self.assertEqual(tracker.icon, "mdi:car")
        self.assertEqual(tracker.unique_id, "veh-1_location")

    def test_real_latitude_with_zero_longitude_passes_through(self) -> None:
        tracker = _tracker(_LIVE)
        self.assertEqual(tracker.latitude, float("51.5007"))
        self.assertEqual(tracker.longitude, 0.0)
        self.assertEqual(
            tracker.extra_state_attributes,
            {
                "altitude": 15.0,
                "heading": 0.0,
                "satellites": 8,
                "gps_time": "2026-01-01 12:00:00",
                "location_switch": True,
            },
        )

    def test_zero_longitude_is_not_rewritten_when_data_changes(self) -> None:
        coordinator = _Coordinator({"locationGeo": dict(_LIVE)})
        tracker = DeepalLocationTracker(coordinator)
        self.assertEqual((tracker.latitude, tracker.longitude), (float("51.5007"), 0.0))
        coordinator.data = {
            "condition": {"locationGeo": {**_LIVE, "lat": "51.5", "lng": "0.0"}},
            "vehicle": {},
        }
        self.assertEqual((tracker.latitude, tracker.longitude), (51.5, 0.0))

    def test_zero_latitude_with_real_longitude_passes_through(self) -> None:
        tracker = _tracker({**_LIVE, "lat": "0.0", "lng": "1.2"})
        self.assertEqual((tracker.latitude, tracker.longitude), (0.0, 1.2))

    def test_unavailable_when_uninitialised_or_off(self) -> None:
        cases = {
            "missing": None,
            "not a dict": "48.8,2.3",
            "switch off": {**_LIVE, "locationSwitch": 0},
            "switch off string": {**_LIVE, "locationSwitch": "0"},
            "sentinel time": {**_LIVE, "gpsTime": "2000-01-01 00:00:00"},
            "blank time": {**_LIVE, "gpsTime": "  "},
            "missing time": {key: value for key, value in _LIVE.items() if key != "gpsTime"},
            "both zero": {**_LIVE, "lat": "0.0", "lng": "0.0"},
            "both zero numbers": {**_LIVE, "lat": 0, "lng": 0.0},
            "unparsed lat": {**_LIVE, "lat": "nope"},
            "non finite": {**_LIVE, "lng": "nan"},
            "cloud off payload": _OFF,
        }
        for name, location_geo in cases.items():
            with self.subTest(name=name):
                tracker = _tracker(location_geo)
                self.assertEqual((tracker.latitude, tracker.longitude), (None, None))

    def test_off_payload_still_exposes_switch(self) -> None:
        attrs = _tracker(_OFF).extra_state_attributes
        self.assertEqual(attrs["altitude"], 0.0)
        self.assertEqual(attrs["satellites"], 0)
        self.assertEqual(attrs["gps_time"], "2000-01-01 00:00:00")
        self.assertIs(attrs["location_switch"], False)


class S05LocationMappingTests(unittest.TestCase):
    def test_maps_location_fields_and_aliases(self) -> None:
        mapped = _s05_condition_from_params(
            {
                "lat": "51.5007",
                "carLocationLatitude": "99",
                "lng": "0.0",
                "alti": "15.0",
                "heading": "0.0",
                "satNum": "8",
                "gpsTime": "2026-01-01 12:00:00",
                "locationSwitch": 1,
            }
        )
        self.assertEqual(
            mapped["locationGeo"],
            {
                "lat": "51.5007",
                "lng": "0.0",
                "alti": "15.0",
                "heading": "0.0",
                "satNum": "8",
                "gpsTime": "2026-01-01 12:00:00",
                "locationSwitch": 1,
            },
        )
        tracker = DeepalLocationTracker(_Coordinator(mapped))
        self.assertEqual((tracker.latitude, tracker.longitude), (float("51.5007"), 0.0))

    def test_car_location_aliases_when_lat_lng_absent(self) -> None:
        mapped = _s05_condition_from_params(
            {
                "carLocationLatitude": "51.5",
                "carLocationLongitude": "-0.12",
                "gpsTime": "2026-11-25 12:00:00",
                "locationSwitch": "1",
            }
        )
        self.assertEqual(mapped["locationGeo"]["lat"], "51.5")
        self.assertEqual(mapped["locationGeo"]["lng"], "-0.12")
        tracker = DeepalLocationTracker(_Coordinator(mapped))
        self.assertEqual((tracker.latitude, tracker.longitude), (51.5, -0.12))

    def test_location_switch_alone_is_mapped(self) -> None:
        mapped = _s05_condition_from_params({"locationSwitch": 0, "soc": 80})
        self.assertEqual(mapped["locationGeo"]["locationSwitch"], 0)
        self.assertIsNone(mapped["locationGeo"]["lat"])

    def test_unrelated_params_omit_location(self) -> None:
        mapped = _s05_condition_from_params({"soc": 80, "totalOdometer": 10})
        self.assertNotIn("locationGeo", mapped)


if __name__ == "__main__":
    unittest.main()

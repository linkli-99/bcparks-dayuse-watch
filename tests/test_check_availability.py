import importlib.util
import json
import os
import sys
import unittest
from datetime import date, datetime, time
from email.message import Message
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest.mock import patch
from urllib.error import URLError


SCRIPT = Path(__file__).parents[1] / "scripts" / "check_availability.py"
SPEC = importlib.util.spec_from_file_location("check_availability", SCRIPT)
monitor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = monitor
SPEC.loader.exec_module(monitor)


class FakeResponse:
    def __init__(self, payload, content_type="application/json", status=200):
        self.status = status
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status

    def read(self):
        return self.payload


class MonitorTests(unittest.TestCase):
    def write_config(self, payload):
        handle = NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(payload, handle)
        handle.close()
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        return handle.name

    def test_fetch_json_accepts_json(self):
        opener = lambda *_args, **_kwargs: FakeResponse({"ok": True})
        self.assertEqual(monitor.fetch_json("https://example.test", opener=opener), {"ok": True})

    def test_fetch_json_rejects_spa_html_even_with_200(self):
        opener = lambda *_args, **_kwargs: FakeResponse(b"<html></html>", "text/html")
        with self.assertRaisesRegex(monitor.MonitorError, "non-JSON"):
            monitor.fetch_json("https://example.test", opener=opener)

    def test_fetch_json_retries_network_error(self):
        calls = []
        sleeps = []

        def opener(*_args, **_kwargs):
            calls.append(True)
            if len(calls) == 1:
                raise URLError("temporary")
            return FakeResponse({"ok": True})

        self.assertEqual(
            monitor.fetch_json("https://example.test", opener=opener, sleeper=sleeps.append),
            {"ok": True},
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [2])

    def test_available_response(self):
        result = monitor.parse_availability(
            {"2026-08-15": {"DAY": {"capacity": "Low", "max": 1}}},
            date(2026, 8, 15),
            "DAY",
        )
        self.assertTrue(result.available)

    def test_full_response(self):
        result = monitor.parse_availability(
            {"2026-08-15": {"DAY": {"capacity": "Full", "max": 0}}},
            date(2026, 8, 15),
            "DAY",
        )
        self.assertFalse(result.available)

    def test_conflicting_response_fails_closed(self):
        with self.assertRaisesRegex(monitor.MonitorError, "disagree"):
            monitor.parse_availability(
                {"2026-08-15": {"DAY": {"capacity": "Full", "max": 1}}},
                date(2026, 8, 15),
                "DAY",
            )

    def test_rubble_creek_weekday_rules(self):
        facility = {
            "bookingDays": {"1": True, "2": False, "3": False, "4": False,
                            "5": True, "6": True, "7": True},
            "bookableHolidays": {},
        }
        self.assertFalse(monitor.pass_is_required(facility, date(2026, 8, 13)))  # Thursday
        self.assertTrue(monitor.pass_is_required(facility, date(2026, 8, 15)))   # Saturday

    def test_config_supports_multiple_locations_with_one_date(self):
        path = self.write_config({
            "visit_date": "2026-08-15",
            "stop_after_local_time": "09:30",
            "subscriptions": [
                {"label": "Rubble", "park_id": "0007", "facility": "Rubble Creek", "slot": "DAY"},
                {"label": "Joffre", "park_id": "0363", "facility": "Joffre Lakes", "slot": "DAY"},
            ],
        })
        config = monitor.load_config(path)
        self.assertEqual(config.visit_date, date(2026, 8, 15))
        self.assertEqual(config.stop_after_local_time, time(9, 30))
        self.assertEqual([item.park_id for item in config.subscriptions], ["0007", "0363"])
        self.assertTrue(all(item.booking_url == monitor.BOOKING_URL for item in config.subscriptions))

    def test_config_accepts_safe_park_links(self):
        path = self.write_config({
            "visit_date": "2026-08-15",
            "subscriptions": [{
                "park_id": "0007",
                "facility": "Rubble Creek",
                "booking_url": "https://reserve.bcparks.ca/dayuse/",
                "park_url": "https://bcparks.ca/garibaldi-park/",
            }],
        })
        subscription = monitor.load_config(path).subscriptions[0]
        self.assertEqual(subscription.park_url, "https://bcparks.ca/garibaldi-park/")

    def test_config_rejects_untrusted_notification_link(self):
        path = self.write_config({
            "visit_date": "2026-08-15",
            "subscriptions": [{
                "park_id": "0007",
                "facility": "Rubble Creek",
                "booking_url": "https://example.com/not-bc-parks",
            }],
        })
        with self.assertRaisesRegex(monitor.MonitorError, "booking_url"):
            monitor.load_config(path)

    def test_config_rejects_per_location_dates(self):
        path = self.write_config({
            "visit_date": "2026-08-15",
            "subscriptions": [
                {"park_id": "0007", "facility": "Rubble Creek", "date": "2026-08-16"},
            ],
        })
        with self.assertRaisesRegex(monitor.MonitorError, "share visit_date"):
            monitor.load_config(path)

    def test_disabled_locations_are_not_subscribed(self):
        path = self.write_config({
            "visit_date": "2026-08-15",
            "subscriptions": [
                {"enabled": True, "park_id": "0007", "facility": "Rubble Creek"},
                {"enabled": False, "park_id": "0363", "facility": "Joffre Lakes"},
            ],
        })
        config = monitor.load_config(path)
        self.assertEqual(len(config.subscriptions), 1)
        self.assertEqual(config.subscriptions[0].facility, "Rubble Creek")

    def test_time_check_expires_after_configured_target_time(self):
        config = monitor.MonitorConfig(
            date(2026, 8, 15),
            time(9, 0),
            (monitor.Subscription("Rubble", "0007", "Rubble Creek", "DAY"),),
        )
        before = datetime(2026, 8, 15, 8, 59, tzinfo=monitor.PACIFIC)
        after = datetime(2026, 8, 15, 9, 1, tzinfo=monitor.PACIFIC)
        self.assertFalse(monitor.monitoring_has_expired(config, before))
        self.assertTrue(monitor.monitoring_has_expired(config, after))

    def test_prefetched_results_are_validated_and_indexed(self):
        config = monitor.MonitorConfig(
            date(2026, 8, 15),
            time(23, 59),
            (monitor.Subscription("Rubble", "0007", "Rubble Creek", "DAY"),),
        )
        payload = json.dumps({
            "schema_version": 1,
            "visit_date": "2026-08-15",
            "locations": [{
                "park_id": "0007",
                "facility": "Rubble Creek",
                "facilities": [],
                "reservation": {},
            }],
        })
        indexed = monitor.load_prefetched_results(payload, config)
        self.assertIn(("0007", "Rubble Creek"), indexed)

    def test_prefetched_results_reject_wrong_date(self):
        config = monitor.MonitorConfig(
            date(2026, 8, 15),
            time(23, 59),
            (monitor.Subscription("Rubble", "0007", "Rubble Creek", "DAY"),),
        )
        payload = json.dumps({"schema_version": 1, "visit_date": "2026-08-16", "locations": []})
        with self.assertRaisesRegex(monitor.MonitorError, "date does not match"):
            monitor.load_prefetched_results(payload, config)

    def test_process_subscription_uses_prefetched_reservation_without_network(self):
        subscription = monitor.Subscription("Rubble", "0007", "Rubble Creek", "DAY")
        config = monitor.MonitorConfig(date(2026, 8, 15), time(23, 59), (subscription,))
        facilities = [{
            "name": "Rubble Creek",
            "visible": True,
            "status": {"state": "open"},
            "bookingTimes": {"DAY": True},
            "bookingDays": {str(day): True for day in range(1, 8)},
            "bookableHolidays": {},
            "bookingDaysAhead": 2,
            "bookingOpeningHour": 7,
        }]
        reservation = {"2026-08-15": {"DAY": {"capacity": "Full", "max": 0}}}
        with patch.object(monitor, "fetch_json") as fetch:
            result = monitor.process_subscription(
                subscription,
                config,
                facilities,
                datetime(2026, 8, 13, 8, 0, tzinfo=monitor.PACIFIC),
                False,
                {"notified": {}},
                reservation,
            )
        fetch.assert_not_called()
        self.assertEqual(result["status"], "full")

    def test_ntfy_publish_uses_post_without_exposing_topic(self):
        captured = {}

        def opener(request, **_kwargs):
            captured["url"] = request.full_url
            captured["method"] = request.method
            captured["headers"] = dict(request.header_items())
            captured["body"] = request.data.decode()
            return FakeResponse({"id": "ok"}, status=200)

        availability = monitor.Availability("2026-08-15", "DAY", "Low", 1)
        subscription = monitor.Subscription(
            "Rubble Creek",
            "0007",
            "Rubble Creek",
            "DAY",
            monitor.BOOKING_URL,
            "https://bcparks.ca/garibaldi-park/",
        )
        with patch.dict(os.environ, {"NTFY_TOPIC": "random_topic_123456"}, clear=False), patch.object(
            monitor, "urlopen", opener
        ):
            monitor.publish_ntfy(availability, subscription)
        self.assertEqual(captured["method"], "POST")
        self.assertTrue(captured["url"].endswith("/random_topic_123456"))
        self.assertEqual(captured["headers"]["Click"], monitor.BOOKING_URL)
        self.assertIn("Book now", captured["headers"]["Actions"])
        self.assertIn("Park details", captured["headers"]["Actions"])
        self.assertIn(monitor.BOOKING_URL, captured["body"])

    def test_notification_state_sends_once_then_rearms_after_full(self):
        subscription = monitor.Subscription("Rubble", "0007", "Rubble Creek", "DAY")
        available = monitor.Availability("2026-08-15", "DAY", "Low", 1)
        full = monitor.Availability("2026-08-15", "DAY", "Full", 0)
        state = {"notified": {}}

        with patch.object(monitor, "publish_ntfy") as publish:
            self.assertEqual(
                monitor.update_notification_state(available, subscription, date(2026, 8, 15), state),
                ("sent", True),
            )
            self.assertEqual(
                monitor.update_notification_state(available, subscription, date(2026, 8, 15), state),
                ("suppressed", False),
            )
            self.assertEqual(publish.call_count, 1)

            self.assertEqual(
                monitor.update_notification_state(full, subscription, date(2026, 8, 15), state),
                ("rearmed", True),
            )
            self.assertEqual(
                monitor.update_notification_state(available, subscription, date(2026, 8, 15), state),
                ("sent", True),
            )
            self.assertEqual(publish.call_count, 2)

    def test_dry_run_does_not_persist_notification_state(self):
        subscription = monitor.Subscription("Rubble", "0007", "Rubble Creek", "DAY")
        available = monitor.Availability("2026-08-15", "DAY", "Low", 1)
        state = {"notified": {}}
        with patch.dict(os.environ, {"BCPARKS_DRY_RUN": "1"}, clear=False), patch.object(
            monitor, "publish_ntfy"
        ) as publish:
            result = monitor.update_notification_state(
                available, subscription, date(2026, 8, 15), state
            )
        self.assertEqual(result, ("dry_run", False))
        publish.assert_called_once()
        self.assertEqual(state, {"notified": {}})

    def test_prune_state_removes_old_dates_and_disabled_locations(self):
        subscription = monitor.Subscription("Rubble", "0007", "Rubble Creek", "DAY")
        config = monitor.MonitorConfig(date(2026, 8, 15), time(23, 59), (subscription,))
        current = monitor.subscription_key(subscription, config.visit_date)
        state = {"notified": {current: {}, "2026-08-08|0363|Joffre Lakes|DAY": {}}}
        self.assertTrue(monitor.prune_state(state, config))
        self.assertEqual(list(state["notified"]), [current])


if __name__ == "__main__":
    unittest.main()

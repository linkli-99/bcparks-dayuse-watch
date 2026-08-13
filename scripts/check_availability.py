#!/usr/bin/env python3
"""Read-only, multi-location BC Parks day-use availability monitor.

All enabled locations share one target date. The script intentionally does not
hold or book passes; it only reads the public data used by the registration UI.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from email.message import Message
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


API_BASE = os.getenv("BCPARKS_API_BASE", "https://reserve.bcparks.ca/api").rstrip("/")
REGISTRATION_REFERER = "https://reserve.bcparks.ca/dayuse/registration"
BOOKING_URL = "https://reserve.bcparks.ca/dayuse/"
PACIFIC = ZoneInfo("America/Vancouver")
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
DEFAULT_CONFIG_PATH = "config/subscriptions.json"
DEFAULT_STATE_PATH = "state.json"


class MonitorError(RuntimeError):
    """Raised when configuration or an upstream response cannot be trusted."""


@dataclass(frozen=True)
class Subscription:
    label: str
    park_id: str
    facility: str
    slot: str
    booking_url: str = BOOKING_URL
    park_url: str = ""


@dataclass(frozen=True)
class MonitorConfig:
    visit_date: date
    stop_after_local_time: datetime_time
    subscriptions: tuple[Subscription, ...]

    @property
    def stop_at(self) -> datetime:
        return datetime.combine(self.visit_date, self.stop_after_local_time, tzinfo=PACIFIC)


@dataclass(frozen=True)
class Availability:
    visit_date: str
    slot: str
    capacity: str
    maximum_bookable: int

    @property
    def available(self) -> bool:
        return self.maximum_bookable > 0 and self.capacity.lower() not in {
            "full",
            "unavailable",
        }


def parse_visit_date(raw: str) -> date:
    if not raw:
        raise MonitorError("visit_date is required and must use YYYY-MM-DD.")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise MonitorError("visit_date must use YYYY-MM-DD.") from exc


def parse_stop_time(raw: str) -> datetime_time:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw or ""):
        raise MonitorError("stop_after_local_time must use 24-hour HH:MM in America/Vancouver.")
    hour, minute = (int(part) for part in raw.split(":"))
    return datetime_time(hour, minute)


def validate_public_url(raw: str, field: str, allowed_hosts: set[str]) -> str:
    parsed = urlparse(raw)
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        hosts = ", ".join(sorted(allowed_hosts))
        raise MonitorError(f"{field} must be an HTTPS URL on one of: {hosts}.")
    return raw


def load_config(path: str | Path, visit_date_override: str = "") -> MonitorConfig:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MonitorError(f"Subscription file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise MonitorError(f"Subscription file is invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise MonitorError("Subscription file must contain a JSON object.")

    visit_date = parse_visit_date(visit_date_override or str(payload.get("visit_date", "")))
    stop_time = parse_stop_time(str(payload.get("stop_after_local_time", "23:59")))
    raw_subscriptions = payload.get("subscriptions")
    if not isinstance(raw_subscriptions, list):
        raise MonitorError("subscriptions must be a JSON list.")

    subscriptions: list[Subscription] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(raw_subscriptions):
        if not isinstance(raw, dict):
            raise MonitorError(f"subscriptions[{index}] must be a JSON object.")
        if "date" in raw or "visit_date" in raw:
            raise MonitorError("Location entries cannot define dates; all locations share visit_date.")
        if raw.get("enabled", True) is False:
            continue
        park_id = str(raw.get("park_id", "")).strip()
        facility = str(raw.get("facility", "")).strip()
        slot = str(raw.get("slot", "DAY")).strip().upper()
        label = str(raw.get("label", facility)).strip()
        booking_url = validate_public_url(
            str(raw.get("booking_url", BOOKING_URL)).strip(),
            f"subscriptions[{index}].booking_url",
            {"reserve.bcparks.ca"},
        )
        park_url_raw = str(raw.get("park_url", "")).strip()
        park_url = (
            validate_public_url(
                park_url_raw,
                f"subscriptions[{index}].park_url",
                {"bcparks.ca", "www.bcparks.ca"},
            )
            if park_url_raw
            else ""
        )
        if not re.fullmatch(r"\d{4}", park_id):
            raise MonitorError(f"subscriptions[{index}].park_id must contain four digits.")
        if not facility or len(facility) > 120:
            raise MonitorError(f"subscriptions[{index}].facility is invalid.")
        if not re.fullmatch(r"[A-Z][A-Z0-9_-]{0,19}", slot):
            raise MonitorError(f"subscriptions[{index}].slot is invalid.")
        if not label or len(label) > 120:
            raise MonitorError(f"subscriptions[{index}].label is invalid.")
        key = (park_id, facility, slot)
        if key in seen:
            raise MonitorError(f"Duplicate enabled subscription: {park_id}/{facility}/{slot}.")
        seen.add(key)
        subscriptions.append(Subscription(label, park_id, facility, slot, booking_url, park_url))
    if not subscriptions:
        raise MonitorError("At least one subscription must be enabled.")
    return MonitorConfig(visit_date, stop_time, tuple(subscriptions))


def request_headers() -> dict[str, str]:
    repository = os.getenv("GITHUB_REPOSITORY", "local")
    return {
        "Accept": "application/json, text/plain, */*",
        # The deployed endpoint currently requires same-site request context.
        # Use only the public registration page and never invent user identity.
        "Referer": REGISTRATION_REFERER,
        "User-Agent": f"bcparks-read-only-availability-monitor/2.0 ({repository})",
    }


def _content_type(headers: Message | Any) -> str:
    if hasattr(headers, "get_content_type"):
        return headers.get_content_type()
    raw = headers.get("Content-Type", "") if headers else ""
    return str(raw).split(";", 1)[0].strip().lower()


def fetch_json(
    url: str,
    *,
    attempts: int = 3,
    timeout: float = 12,
    opener: Callable[..., Any] = urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> Any:
    """Fetch JSON with bounded retry and strict content validation."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = Request(url, headers=request_headers(), method="GET")
        try:
            with opener(request, timeout=timeout) as response:
                status = getattr(response, "status", response.getcode())
                content_type = _content_type(response.headers)
                payload = response.read()
            if status != 200:
                raise MonitorError(f"Unexpected HTTP status {status} from BC Parks.")
            if content_type != "application/json":
                raise MonitorError(
                    "BC Parks returned non-JSON content. The API path or access policy may "
                    "have changed; refusing to infer availability."
                )
            try:
                return json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise MonitorError("BC Parks returned invalid JSON.") from exc
        except HTTPError as exc:
            last_error = exc
            if exc.code not in RETRYABLE_STATUS or attempt == attempts:
                raise MonitorError(f"BC Parks returned HTTP {exc.code}.") from exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            delay = min(float(retry_after), 30.0) if retry_after and retry_after.isdigit() else 2**attempt
        except (URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            if attempt == attempts:
                raise MonitorError("Unable to reach BC Parks after bounded retries.") from exc
            delay = 2**attempt
        sleeper(delay)
    raise MonitorError("Unable to reach BC Parks.") from last_error


def find_facility(payload: Any, name: str) -> dict[str, Any]:
    if not isinstance(payload, list):
        raise MonitorError("Facility response is not a JSON list.")
    matches = [item for item in payload if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1:
        raise MonitorError(f"Expected exactly one facility named {name!r}; found {len(matches)}.")
    facility = matches[0]
    if facility.get("visible") is False or facility.get("status", {}).get("state") != "open":
        raise MonitorError(f"Facility {name!r} is not open and visible.")
    return facility


def holiday_dates(raw: Any) -> set[str]:
    if isinstance(raw, list):
        return {str(item) for item in raw}
    if isinstance(raw, dict):
        return {str(key) for key, enabled in raw.items() if enabled is not False}
    return set()


def pass_is_required(facility: dict[str, Any], visit_date: date) -> bool:
    weekdays = facility.get("bookingDays")
    if not isinstance(weekdays, dict):
        raise MonitorError("Facility response is missing bookingDays.")
    weekday_required = bool(weekdays.get(str(visit_date.isoweekday()), False))
    return weekday_required or visit_date.isoformat() in holiday_dates(facility.get("bookableHolidays"))


def release_time(facility: dict[str, Any], visit_date: date) -> datetime:
    days_ahead = facility.get("bookingDaysAhead", 2)
    opening_hour = facility.get("bookingOpeningHour", 7)
    if not isinstance(days_ahead, int) or not 0 <= days_ahead <= 30:
        raise MonitorError("Facility response has an invalid bookingDaysAhead value.")
    if not isinstance(opening_hour, int) or not 0 <= opening_hour <= 23:
        raise MonitorError("Facility response has an invalid bookingOpeningHour value.")
    local_open = datetime.combine(visit_date, datetime_time(opening_hour), tzinfo=PACIFIC)
    return local_open - timedelta(days=days_ahead)


def parse_availability(payload: Any, visit_date: date, slot: str) -> Availability:
    day = payload.get(visit_date.isoformat()) if isinstance(payload, dict) else None
    item = day.get(slot) if isinstance(day, dict) else None
    if not isinstance(item, dict):
        raise MonitorError(f"Availability response is missing {visit_date.isoformat()} / {slot}.")
    capacity = item.get("capacity")
    maximum = item.get("max")
    if not isinstance(capacity, str) or not isinstance(maximum, int) or maximum < 0:
        raise MonitorError("Availability response has an unexpected schema.")
    unavailable_label = capacity.lower() in {"full", "unavailable"}
    if (maximum > 0) == unavailable_label:
        raise MonitorError(
            f"Availability signals disagree (capacity={capacity!r}, max={maximum}); refusing a false alert."
        )
    return Availability(visit_date.isoformat(), slot, capacity, maximum)


def emit_github_output(name: str, value: str) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def load_state(path: str | Path) -> dict[str, Any]:
    state_path = Path(path)
    if not state_path.exists():
        return {"notified": {}}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MonitorError(f"State file is invalid JSON: {exc}") from exc
    if not isinstance(state, dict) or not isinstance(state.get("notified", {}), dict):
        raise MonitorError("State file must contain a 'notified' JSON object.")
    state.setdefault("notified", {})
    return state


def save_state(path: str | Path, state: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def subscription_key(subscription: Subscription, visit_date: date) -> str:
    return "|".join(
        (visit_date.isoformat(), subscription.park_id, subscription.facility, subscription.slot)
    )


def prune_state(state: dict[str, Any], config: MonitorConfig) -> bool:
    active_keys = {subscription_key(item, config.visit_date) for item in config.subscriptions}
    notified = state.setdefault("notified", {})
    stale = [key for key in notified if key not in active_keys]
    for key in stale:
        del notified[key]
    return bool(stale)


def ntfy_headers(title: str, click_url: str, park_url: str = "") -> dict[str, str]:
    actions = [f"view, Book now, {click_url}, clear=true"]
    if park_url:
        actions.append(f"view, Park details, {park_url}")
    headers = {
        "Title": title,
        "Priority": "5",
        "Tags": "tada,hiking",
        "Click": click_url,
        "Actions": "; ".join(actions),
        "Content-Type": "text/plain; charset=utf-8",
        "User-Agent": "bcparks-read-only-availability-monitor/3.0",
    }
    token = os.getenv("NTFY_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def send_ntfy(title: str, message: str, click_url: str, park_url: str = "") -> None:
    if os.getenv("BCPARKS_DRY_RUN", "").lower() in {"1", "true", "yes"}:
        print(f"[dry-run] ntfy title={title!r} click={click_url!r} message={message!r}")
        return
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        raise MonitorError("NTFY_TOPIC is not configured; refusing to lose an availability alert.")
    if not re.fullmatch(r"[-_A-Za-z0-9]{8,64}", topic):
        raise MonitorError("NTFY_TOPIC must be an unguessable 8-64 character ntfy topic.")
    server = (os.getenv("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    parsed = urlparse(server)
    if parsed.scheme != "https" or not parsed.netloc:
        raise MonitorError("NTFY_SERVER must be an HTTPS URL.")
    request = Request(
        f"{server}/{quote(topic, safe='')}",
        data=message.encode("utf-8"),
        headers=ntfy_headers(title, click_url, park_url),
        method="POST",
    )
    try:
        with urlopen(request, timeout=12) as response:
            if response.status not in {200, 201}:
                raise MonitorError(f"ntfy returned HTTP {response.status}.")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise MonitorError("ntfy notification failed.") from exc


def publish_ntfy(availability: Availability, subscription: Subscription) -> None:
    message = (
        f"{subscription.label} has {availability.capacity.lower()} availability for "
        f"{availability.visit_date} ({availability.slot}); up to "
        f"{availability.maximum_bookable} currently bookable.\n\n"
        f"Book now: {subscription.booking_url}"
    )
    if subscription.park_url:
        message += f"\nPark details: {subscription.park_url}"
    send_ntfy(
        f"BC Parks pass available: {subscription.label}",
        message,
        subscription.booking_url,
        subscription.park_url,
    )
    print(f"{subscription.label}: push notification sent.")


def publish_test_ntfy(subscription: Subscription, visit_date: date) -> None:
    send_ntfy(
        "BC Parks monitor test",
        f"Notifications are working for {subscription.label} on {visit_date.isoformat()}.\n\n"
        f"Booking site: {subscription.booking_url}",
        subscription.booking_url,
        subscription.park_url,
    )
    print("Test push notification sent.")


def update_notification_state(
    availability: Availability,
    subscription: Subscription,
    visit_date: date,
    state: dict[str, Any],
) -> tuple[str, bool]:
    key = subscription_key(subscription, visit_date)
    notified = state.setdefault("notified", {})
    if availability.available:
        if key in notified:
            return "suppressed", False
        publish_ntfy(availability, subscription)
        if os.getenv("BCPARKS_DRY_RUN", "").lower() in {"1", "true", "yes"}:
            return "dry_run", False
        notified[key] = {
            "label": subscription.label,
            "notified_at": datetime.now(PACIFIC).isoformat(),
        }
        return "sent", True
    if key in notified:
        del notified[key]
        return "rearmed", True
    return "none", False


def coarse_monitoring_start(visit_date: date) -> datetime:
    days_hint = int(os.getenv("BCPARKS_BOOKING_DAYS_AHEAD_HINT", "2"))
    hour_hint = int(os.getenv("BCPARKS_BOOKING_OPENING_HOUR_HINT", "7"))
    if not 0 <= days_hint <= 30 or not 0 <= hour_hint <= 23:
        raise MonitorError("Booking-window hints are invalid.")
    return (
        datetime.combine(visit_date, datetime_time(hour_hint), tzinfo=PACIFIC)
        - timedelta(days=days_hint, minutes=10)
    )


def monitoring_has_expired(config: MonitorConfig, now: datetime) -> bool:
    """Return true only after the configured local stop time on the shared date."""
    return now > config.stop_at


def process_subscription(
    subscription: Subscription,
    config: MonitorConfig,
    facilities_payload: Any,
    now: datetime,
    force_check: bool,
    state: dict[str, Any],
) -> dict[str, Any]:
    facility = find_facility(facilities_payload, subscription.facility)
    if subscription.slot not in facility.get("bookingTimes", {}):
        raise MonitorError(f"Facility {subscription.facility!r} does not offer slot {subscription.slot!r}.")
    if not pass_is_required(facility, config.visit_date):
        return {"label": subscription.label, "status": "not_required"}
    opens = release_time(facility, config.visit_date)
    if now < opens and not force_check:
        return {"label": subscription.label, "status": "before_release", "opens": opens.isoformat()}

    query = urlencode(
        {"park": subscription.park_id, "facility": subscription.facility, "date": config.visit_date.isoformat()}
    )
    payload = fetch_json(f"{API_BASE}/reservation?{query}")
    availability = parse_availability(payload, config.visit_date, subscription.slot)
    notification, state_changed = update_notification_state(
        availability, subscription, config.visit_date, state
    )
    return {
        "label": subscription.label,
        "status": "available" if availability.available else "full",
        "capacity": availability.capacity,
        "max": availability.maximum_bookable,
        "notification": notification,
        "_state_changed": state_changed,
    }


def summarize_results(results: list[dict[str, Any]]) -> tuple[str, int]:
    available_count = sum(result.get("status") == "available" for result in results)
    checked_count = sum(result.get("status") in {"available", "full"} for result in results)
    error_count = sum(result.get("status") == "error" for result in results)
    emit_github_output("available", str(available_count > 0).lower())
    emit_github_output("available_count", str(available_count))
    emit_github_output("checked_count", str(checked_count))
    emit_github_output("error_count", str(error_count))
    emit_github_output("results_json", json.dumps(results, separators=(",", ":")))

    if error_count:
        status = "partial_error" if len(results) > error_count else "error"
        exit_code = 1
    elif available_count:
        status = "available"
        exit_code = 0
    elif checked_count:
        status = "full"
        exit_code = 0
    elif any(result.get("status") == "before_release" for result in results):
        status = "before_release"
        exit_code = 0
    else:
        status = "idle"
        exit_code = 0
    emit_github_output("status", status)
    return status, exit_code


def main() -> int:
    try:
        config_path = os.getenv("BCPARKS_SUBSCRIPTIONS_FILE", DEFAULT_CONFIG_PATH)
        config = load_config(config_path, os.getenv("BCPARKS_VISIT_DATE", "").strip())
        force_check = os.getenv("BCPARKS_FORCE_CHECK", "").lower() in {"1", "true", "yes"}
        now = datetime.now(PACIFIC)

        if os.getenv("BCPARKS_TEST_NOTIFICATION", "").lower() in {"1", "true", "yes"}:
            publish_test_ntfy(config.subscriptions[0], config.visit_date)
            emit_github_output("status", "test_sent")
            emit_github_output("available", "false")
            emit_github_output("available_count", "0")
            emit_github_output("checked_count", "0")
            emit_github_output("error_count", "0")
            return 0

        # This check happens before any network request.
        if monitoring_has_expired(config, now) and not force_check:
            print(f"Monitoring expired at {config.stop_at.isoformat()}; no request sent.")
            emit_github_output("status", "expired")
            emit_github_output("available", "false")
            emit_github_output("available_count", "0")
            emit_github_output("checked_count", "0")
            emit_github_output("error_count", "0")
            return 0

        start_at = coarse_monitoring_start(config.visit_date)
        if now < start_at and not force_check:
            print(f"Monitoring begins around {start_at.isoformat()}; no request sent.")
            emit_github_output("status", "before_release")
            emit_github_output("available", "false")
            emit_github_output("available_count", "0")
            emit_github_output("checked_count", "0")
            emit_github_output("error_count", "0")
            return 0

        state_path = os.getenv("BCPARKS_STATE_FILE", DEFAULT_STATE_PATH)
        state = load_state(state_path)
        state_changed = prune_state(state, config)

        by_park: dict[str, list[Subscription]] = {}
        for subscription in config.subscriptions:
            by_park.setdefault(subscription.park_id, []).append(subscription)

        results: list[dict[str, Any]] = []
        for park_id, subscriptions in by_park.items():
            try:
                facility_query = urlencode({"park": park_id, "facilities": "true"})
                facilities = fetch_json(f"{API_BASE}/facility?{facility_query}")
            except MonitorError as exc:
                for subscription in subscriptions:
                    results.append({"label": subscription.label, "status": "error", "error": str(exc)})
                continue

            for subscription in subscriptions:
                try:
                    result = process_subscription(
                        subscription, config, facilities, now, force_check, state
                    )
                    state_changed = bool(result.pop("_state_changed", False)) or state_changed
                    results.append(result)
                except MonitorError as exc:
                    results.append({"label": subscription.label, "status": "error", "error": str(exc)})

        if state_changed:
            save_state(state_path, state)
            print(f"Notification state updated in {state_path}.")

        for result in results:
            detail = ", ".join(f"{key}={value}" for key, value in result.items() if key != "label")
            print(f"{result['label']}: {detail}")
        status, exit_code = summarize_results(results)
        print(f"Overall status: {status}")
        return exit_code
    except (MonitorError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        emit_github_output("status", "error")
        emit_github_output("available", "false")
        emit_github_output("error_count", "1")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

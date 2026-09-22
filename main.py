"""TopDrive CRM practice-lesson sniper.

The CRM is an Inertia.js app: every page ships its server-side props as JSON in
the `data-page` attribute of `#app`. We read the slot list straight from there
instead of scraping rendered markup, and book with the same POST the app's own
"confirm" button issues. No CSS selectors to keep in sync beyond the login form.

Logs in once, then re-checks availability on a jittered interval and books the
first eligible slot. Designed to run for a bounded window (RUN_DURATION_SECONDS)
so a scheduler can restart it.
"""

import json
import os
import random
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

# Local runs read .env; in CI the values come from the workflow env / secrets.
load_dotenv()

BASE_URL = os.environ.get("TOPDRIVE_BASE_URL", "https://crm.topdrive.sk").rstrip("/")
LOGIN_URL = f"{BASE_URL}/auth/login"
AVAILABLE_URL = f"{BASE_URL}/student/practice-lessons/available"

USERNAME = os.environ.get("TOPDRIVE_USERNAME", "")
PASSWORD = os.environ.get("TOPDRIVE_PASSWORD", "")

# The CRM shows slot times in Slovak local time; the runner's clock is UTC.
TZ = ZoneInfo(os.environ.get("TOPDRIVE_TZ", "Europe/Bratislava"))
MIN_HOURS_AHEAD = float(os.environ.get("MIN_HOURS_AHEAD", "8"))

# "any" books whatever is offered; "city" skips training-ground slots; any other
# value is matched against the slot's own lessonType string.
LESSON_TYPE = os.environ.get("LESSON_TYPE", "any").strip().lower()
TRAINING_GROUND = "training_ground"

POLL_MIN_SECONDS = float(os.environ.get("POLL_MIN_SECONDS", "15"))
POLL_MAX_SECONDS = float(os.environ.get("POLL_MAX_SECONDS", "30"))
RUN_DURATION_SECONDS = float(os.environ.get("RUN_DURATION_SECONDS", "540"))
HEADLESS = os.environ.get("HEADLESS", "1") != "0"
# Find and report an eligible slot without actually booking it.
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

# Fallbacks only; the API serves ISO 8601, handled by fromisoformat first.
SLOT_TIME_FORMATS = ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S")


def log(message: str) -> None:
    stamp = datetime.now(TZ).strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def announce(message: str) -> None:
    """Log, and also surface the line in the GitHub Actions run summary."""
    log(message)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"{message}\n\n")


def login(page: Page) -> None:
    log("Logging in...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.fill("input[name='email']", USERNAME)
    page.fill("input[name='password']", PASSWORD)
    page.click("button[type='submit']")
    # The post-login landing page isn't guaranteed to be the dashboard; all we
    # require is that we're no longer sitting on the login form.
    page.wait_for_url(lambda url: "/auth/login" not in url, timeout=30_000)
    log("Login successful, session established.")


def read_page_props(page: Page, url: str) -> dict:
    """Navigate to `url` and return the Inertia page object embedded in the HTML.

    Inertia renders `data-page` server-side, so domcontentloaded is enough — we
    never need to wait for React to paint.
    """
    page.goto(url, wait_until="domcontentloaded")
    raw = page.get_attribute("#app", "data-page")
    if not raw:
        raise PlaywrightError(f"No Inertia data-page payload at {page.url}")
    return json.loads(raw)


def load_availability(page: Page) -> dict:
    """Return the availability page object, re-authenticating if the session died."""
    payload = read_page_props(page, AVAILABLE_URL)
    if payload.get("component") == "auth/login" or "/auth/login" in page.url:
        login(page)
        payload = read_page_props(page, AVAILABLE_URL)
    return payload


def collect_slots(props: dict) -> list[dict]:
    """Flatten the page's slot props into a unique list.

    The component receives both `availableSlots` and `groupedSlots`; the latter
    may arrive grouped by day. Walk whatever is present and dedupe on id so a
    change in which prop is populated can't blind us.
    """
    slots: dict[object, dict] = {}
    for key in ("availableSlots", "groupedSlots"):
        value = props.get(key)
        groups = value.values() if isinstance(value, dict) else [value]
        for group in groups:
            if not isinstance(group, list):
                continue
            for slot in group:
                if isinstance(slot, dict) and "time" in slot:
                    slots.setdefault(slot.get("id", id(slot)), slot)
    return list(slots.values())


def parse_slot_time(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in SLOT_TIME_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    # Naive values are wall-clock times in the school's timezone.
    return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)


def lesson_type_allowed(slot: dict) -> bool:
    lesson_type = slot.get("lessonType")
    if LESSON_TYPE == "any":
        return True
    if LESSON_TYPE == "city":
        return lesson_type != TRAINING_GROUND
    return lesson_type == LESSON_TYPE


def describe(slot: dict, slot_time: datetime) -> str:
    lesson_type = slot.get("lessonType") or "?"
    return f"{slot_time:%Y-%m-%d %H:%M} ({lesson_type}, id={slot.get('id')})"


def find_eligible(slots: list[dict]) -> tuple[dict, datetime] | None:
    """Pick the earliest bookable slot at least MIN_HOURS_AHEAD away."""
    threshold = datetime.now(TZ) + timedelta(hours=MIN_HOURS_AHEAD)
    candidates: list[tuple[datetime, dict]] = []
    skipped_full = skipped_locked = skipped_soon = skipped_type = unparsed = 0

    for slot in slots:
        slot_time = parse_slot_time(slot.get("time"))
        if slot_time is None:
            unparsed += 1
            continue
        if (slot.get("capacity") or {}).get("isFull"):
            skipped_full += 1
            continue
        # canBook is false while a priority-booking window blocks this student.
        if not slot.get("canBook", True):
            skipped_locked += 1
            continue
        if not lesson_type_allowed(slot):
            skipped_type += 1
            continue
        if slot_time <= threshold:
            skipped_soon += 1
            continue
        candidates.append((slot_time, slot))

    if not candidates:
        log(
            f"{len(slots)} slot(s): {skipped_full} full, {skipped_locked} locked, "
            f"{skipped_type} wrong type, {skipped_soon} within {MIN_HOURS_AHEAD:g}h, "
            f"{unparsed} unparsable."
        )
        return None

    slot_time, slot = min(candidates, key=lambda pair: pair[0])
    return slot, slot_time


def csrf_headers(context: BrowserContext, version: object) -> dict[str, str]:
    """Mirror the headers the app's axios client sends on a booking POST."""
    headers = {
        "X-Inertia": "true",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "text/html, application/xhtml+xml",
    }
    if isinstance(version, str):
        headers["X-Inertia-Version"] = version
    for cookie in context.cookies(BASE_URL):
        if cookie.get("name") == "XSRF-TOKEN":
            token = unquote(cookie.get("value", ""))
            headers["X-XSRF-TOKEN"] = token
            headers["X-CSRF-TOKEN"] = token
            break
    else:
        log("Warning: no XSRF-TOKEN cookie; the booking POST may be rejected.")
    return headers


def book(page: Page, context: BrowserContext, slot: dict, version: object) -> bool:
    """POST the booking, then confirm the slot really disappeared."""
    slot_id = slot.get("id")
    if slot_id is None:
        log("Eligible slot has no id; cannot book it.")
        return False

    response = context.request.post(
        f"{BASE_URL}/student/practice-lessons/{slot_id}/book",
        headers=csrf_headers(context, version),
        data={},
    )
    if not response.ok:
        log(f"Booking POST for slot {slot_id} returned HTTP {response.status}.")
        return False

    # A 2xx alone doesn't prove the booking stuck — re-read the page and check.
    remaining = {s.get("id") for s in collect_slots(load_availability(page).get("props", {}))}
    if slot_id in remaining:
        log(f"Slot {slot_id} is still listed after booking; treating as failed.")
        return False
    return True


def check_and_book(page: Page, context: BrowserContext) -> datetime | None:
    """One availability poll. Returns the booked slot time, or None."""
    payload = load_availability(page)
    slots = collect_slots(payload.get("props", {}))
    if not slots:
        log("No slots in page props.")
        return None

    found = find_eligible(slots)
    if found is None:
        return None
    slot, slot_time = found

    if DRY_RUN:
        announce(f"[dry run] Would book {describe(slot, slot_time)}.")
        return slot_time

    log(f"Eligible slot {describe(slot, slot_time)} — booking...")
    if not book(page, context, slot, payload.get("version")):
        return None
    return slot_time


def main() -> int:
    if not USERNAME or not PASSWORD:
        log("TOPDRIVE_USERNAME and TOPDRIVE_PASSWORD must be set.")
        return 2

    deadline = time.monotonic() + RUN_DURATION_SECONDS
    consecutive_errors = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        page = context.new_page()

        try:
            try:
                login(page)
            except (PlaywrightError, PlaywrightTimeout) as exc:
                log(f"Login failed: {exc}")
                return 1

            while time.monotonic() < deadline:
                try:
                    booked = check_and_book(page, context)
                    consecutive_errors = 0
                    if booked:
                        if not DRY_RUN:
                            announce(
                                f"✅ Booked practice lesson for "
                                f"{booked:%Y-%m-%d %H:%M} ({TZ.key})."
                            )
                        return 0
                except (PlaywrightError, PlaywrightTimeout, json.JSONDecodeError) as exc:
                    consecutive_errors += 1
                    log(f"Check failed ({consecutive_errors}): {exc}")
                    if consecutive_errors >= 5:
                        log("Too many consecutive failures, giving up this run.")
                        return 1
                    # Back off so a blocked/rate-limited state isn't hammered.
                    time.sleep(min(60, 5 * 2 ** (consecutive_errors - 1)))
                    continue

                wait = random.uniform(POLL_MIN_SECONDS, POLL_MAX_SECONDS)
                if time.monotonic() + wait > deadline:
                    break
                time.sleep(wait)

            log("Run window over, no eligible slot booked. Exiting for the next run.")
            return 0
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())

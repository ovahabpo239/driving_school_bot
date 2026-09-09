"""TopDrive CRM practice-lesson sniper.

Logs in once, then re-checks the availability page on a jittered interval and
books the first slot that starts at least MIN_HOURS_AHEAD from now. Designed to
run for a bounded window (RUN_DURATION_SECONDS) so a scheduler can restart it.
"""

import os
import random
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("TOPDRIVE_BASE_URL", "https://crm.topdrive.sk").rstrip("/")
LOGIN_URL = f"{BASE_URL}/auth/login"
AVAILABLE_URL = f"{BASE_URL}/student/practice-lessons/available"

USERNAME = os.environ.get("TOPDRIVE_USERNAME", "")
PASSWORD = os.environ.get("TOPDRIVE_PASSWORD", "")

# The CRM shows slot times in Slovak local time; the runner's clock is UTC.
TZ = ZoneInfo(os.environ.get("TOPDRIVE_TZ", "Europe/Bratislava"))
MIN_HOURS_AHEAD = float(os.environ.get("MIN_HOURS_AHEAD", "8"))

POLL_MIN_SECONDS = float(os.environ.get("POLL_MIN_SECONDS", "15"))
POLL_MAX_SECONDS = float(os.environ.get("POLL_MAX_SECONDS", "30"))
RUN_DURATION_SECONDS = float(os.environ.get("RUN_DURATION_SECONDS", "540"))
HEADLESS = os.environ.get("HEADLESS", "1") != "0"

# Selectors are best guesses from the original script — override via env once
# you've inspected the real markup.
SLOT_SELECTOR = os.environ.get("SLOT_SELECTOR", ".termin-slot-available")
SLOT_TIME_ATTR = os.environ.get("SLOT_TIME_ATTR", "data-time")
CONFIRM_SELECTOR = os.environ.get("CONFIRM_SELECTOR", "button#confirm-booking")
SLOT_TIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S")


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
    page.wait_for_url("**/student/dashboard**", timeout=30_000)
    log("Login successful, session established.")


def parse_slot_time(raw: str) -> datetime | None:
    raw = raw.strip()
    for fmt in SLOT_TIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def check_and_book(page: Page) -> datetime | None:
    """Load the availability page once. Returns the booked slot time, or None."""
    page.goto(AVAILABLE_URL, wait_until="networkidle")

    # Session cookies expire; the CRM bounces us back to the login form.
    if "/auth/login" in page.url:
        login(page)
        page.goto(AVAILABLE_URL, wait_until="networkidle")

    threshold = datetime.now(TZ) + timedelta(hours=MIN_HOURS_AHEAD)
    slots = page.locator(SLOT_SELECTOR).all()
    if not slots:
        log(f"No slots rendered (selector {SLOT_SELECTOR!r}).")
        return None

    unparsed = 0
    for slot in slots:
        raw = slot.get_attribute(SLOT_TIME_ATTR) or slot.inner_text()
        slot_time = parse_slot_time(raw)
        if slot_time is None:
            unparsed += 1
            continue
        if slot_time <= threshold:
            continue

        log(f"Eligible slot at {slot_time:%Y-%m-%d %H:%M} — booking...")
        slot.click()
        confirm = page.locator(CONFIRM_SELECTOR)
        if confirm.count():
            confirm.first.click()
        page.wait_for_load_state("networkidle")
        return slot_time

    if unparsed:
        log(f"{len(slots)} slot(s) found, {unparsed} with unrecognised time format e.g. {raw!r}.")
    else:
        log(f"{len(slots)} slot(s) found, all within {MIN_HOURS_AHEAD:g}h.")
    return None


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
            login(page)
        except (PlaywrightError, PlaywrightTimeout) as exc:
            log(f"Login failed: {exc}")
            browser.close()
            return 1

        while time.monotonic() < deadline:
            try:
                booked = check_and_book(page)
                consecutive_errors = 0
                if booked:
                    announce(f"✅ Booked practice lesson for {booked:%Y-%m-%d %H:%M} ({TZ.key}).")
                    browser.close()
                    return 0
            except (PlaywrightError, PlaywrightTimeout) as exc:
                consecutive_errors += 1
                log(f"Check failed ({consecutive_errors}): {exc}")
                if consecutive_errors >= 5:
                    log("Too many consecutive failures, giving up this run.")
                    browser.close()
                    return 1
                # Back off on errors so a blocked/rate-limited state isn't hammered.
                time.sleep(min(60, 5 * 2 ** (consecutive_errors - 1)))
                continue

            wait = random.uniform(POLL_MIN_SECONDS, POLL_MAX_SECONDS)
            if time.monotonic() + wait > deadline:
                break
            time.sleep(wait)

        log("Run window over, no eligible slot booked. Exiting for the next run.")
        browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())

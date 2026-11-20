#!/usr/bin/env python3
"""
Standalone Desktop Bot: BookMyShow Ticket Watcher
-------------------------------------------------
Monitors Prasads Multiplex: Hyderabad (PCX Screen) on BookMyShow for
Avengers: Doomsday (English) across Dec 18–20, 2026, and sends a WhatsApp
or SMS alert as soon as booking opens.

Designed to run independently on your desktop (Windows / macOS / Linux):
  - Handles internet disconnections gracefully without crashing
  - Automatically resumes checking when internet reconnects
  - Avoids false positives when BookMyShow redirects unopened dates
  - Supports Free WhatsApp (CallMeBot) or SMS/WhatsApp (Twilio)
  - Dual fetcher: Uses high-performance TLS fingerprint impersonation (curl_cffi)
    to bypass Cloudflare anti-bot checks, with Playwright Chromium as fallback

QUICK SETUP
-----------
1. Configure your alert method in the CONFIG section below (or via env vars or config.json):
   - Free WhatsApp (CallMeBot):
       a) Save +34 644 71 81 99 in your contacts and send it:
          "I allow callmebot to send me messages"
       b) You will receive an API key on WhatsApp.
       c) Set ALERT_MODE = "callmebot", CALLMEBOT_PHONE = "91XXXXXXXXXX",
          and CALLMEBOT_API_KEY = "YOUR_KEY".
   - SMS or WhatsApp via Twilio:
       Set ALERT_MODE = "twilio_sms" (or "twilio_whatsapp") and fill in
       TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, and TWILIO_TO.

2. Test your WhatsApp/SMS alert first:
       python pcx_ticket_bot.py --test-alert

3. Start watching:
       python pcx_ticket_bot.py
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Try importing curl_cffi for Cloudflare-bypassing HTTP requests
try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

# Try importing BeautifulSoup
try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

# Try importing Playwright
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ==============================================================================
# USER CONFIGURATION (Edit these values or set matching environment variables)
# ==============================================================================

# Optional config file support (loads config.json if present in the same directory)
CONFIG_FILE = Path(__file__).resolve().parent / "config.json"
FILE_CONFIG: Dict[str, str] = {}
if CONFIG_FILE.exists():
    try:
        FILE_CONFIG = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        FILE_CONFIG = {}

# Alert method: "callmebot", "telegram", "ntfy", "twilio_sms", "twilio_whatsapp", or "console_only"
ALERT_MODE = os.getenv("BMS_ALERT_MODE", FILE_CONFIG.get("alert_mode", "callmebot"))

# Option A: CallMeBot (Free WhatsApp)
CALLMEBOT_PHONE = os.getenv("CALLMEBOT_PHONE", FILE_CONFIG.get("callmebot_phone", "91XXXXXXXXXX"))  # Country code + number, no '+'
CALLMEBOT_API_KEY = os.getenv("CALLMEBOT_API_KEY", FILE_CONFIG.get("callmebot_api_key", "YOUR_CALLMEBOT_API_KEY"))

# Option B: Telegram (Instant, 100% Free, Never Blocks)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", FILE_CONFIG.get("telegram_bot_token", ""))
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", FILE_CONFIG.get("telegram_chat_id", ""))

# Option C: ntfy.sh (Zero sign-up, instant mobile push notification)
NTFY_TOPIC = os.getenv("NTFY_TOPIC", FILE_CONFIG.get("ntfy_topic", ""))

# Option D: Twilio (SMS or WhatsApp)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", FILE_CONFIG.get("twilio_account_sid", "ACXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"))
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", FILE_CONFIG.get("twilio_auth_token", "YOUR_TWILIO_AUTH_TOKEN"))
TWILIO_FROM = os.getenv("TWILIO_FROM", FILE_CONFIG.get("twilio_from", "+1XXXXXXXXXX"))  # For WhatsApp: "whatsapp:+14155238886"
TWILIO_TO = os.getenv("TWILIO_TO", FILE_CONFIG.get("twilio_to", "+91XXXXXXXXXX"))     # For WhatsApp: "whatsapp:+91XXXXXXXXXX"

# Target movie, cinema, language, screen, and dates
MOVIE_KEYWORDS = ["avengers: doomsday", "avengers doomsday", "doomsday"]
LANGUAGE_KEYWORD = "english"
SCREEN_KEYWORDS = ["pcx screen", "pcx hdr", "pcx"]
TARGET_DATES = [
    ("20261218", "Fri, 18 Dec 2026"),
    ("20261219", "Sat, 19 Dec 2026"),
    ("20261220", "Sun, 20 Dec 2026"),
]
PRASADS_BASE_URL = (
    "https://in.bookmyshow.com/cinemas/hyderabad/"
    "prasads-multiplex-hyderabad/buytickets/PRHN/{date}"
)

# Polling intervals (in seconds)
CHECK_INTERVAL_SECONDS = int(os.getenv("BMS_CHECK_INTERVAL", FILE_CONFIG.get("polling_interval_seconds", 60)))
OFFLINE_RETRY_SECONDS = 30  # Retry every 30s if desktop internet is disconnected

# Browser mode: set False if BookMyShow shows a Cloudflare challenge you want to inspect
HEADLESS = os.getenv("BMS_HEADLESS", "true").lower() != "false"

# Local state file so restarts don't repeat alerts for dates already notified
STATE_FILE = Path(__file__).resolve().parent / "alerted_dates.json"

# ==============================================================================
# LOGGING
# ==============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pcx_ticket_bot")

TIME_PATTERN = re.compile(r"\b(?:0?[1-9]|1[0-2]):[0-5]\d\s?(?:AM|PM)\b", re.IGNORECASE)


# ==============================================================================
# NETWORK & STATE HELPERS
# ==============================================================================

def has_internet_connection(timeout: float = 5.0) -> bool:
    """Quick TCP connectivity check so offline periods never crash the bot."""
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 443)):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def load_alerted_dates() -> Set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("alerted_dates", []))
    except Exception:
        return set()


def save_alerted_dates(dates: Set[str]) -> None:
    try:
        STATE_FILE.write_text(
            json.dumps({"alerted_dates": sorted(dates)}, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("Could not save state file %s: %s", STATE_FILE, exc)


# ==============================================================================
# ALERTING (WHATSAPP / SMS / DESKTOP BEEP)
# ==============================================================================

def play_desktop_alert() -> None:
    """Play a local sound on Windows/macOS/Linux as an extra desktop cue."""
    try:
        if sys.platform.startswith("win"):
            import winsound  # type: ignore
            for _ in range(5):
                winsound.Beep(1200, 300)
                winsound.Beep(1800, 300)
                time.sleep(0.1)
        else:
            for _ in range(3):
                sys.stdout.write("\a")
                sys.stdout.flush()
                time.sleep(0.25)
    except Exception:
        pass


def send_callmebot_whatsapp(message: str) -> bool:
    """Send free WhatsApp message to user's phone via CallMeBot API."""
    phone = CALLMEBOT_PHONE.strip().replace("+", "")
    api_key = CALLMEBOT_API_KEY.strip()

    if "XXXX" in phone or "YOUR_" in api_key or not phone or not api_key:
        log.error("CallMeBot is not configured. Please set CALLMEBOT_PHONE and CALLMEBOT_API_KEY in pcx_ticket_bot.py or config.json.")
        return False

    query = urllib.parse.urlencode({
        "phone": phone,
        "text": message,
        "apikey": api_key,
    })
    url = f"https://api.callmebot.com/whatsapp.php?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "PCXTicketBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if 200 <= resp.status < 300:
                log.info("WhatsApp alert sent via CallMeBot: %s", body.strip()[:120])
                return True
            log.error("CallMeBot returned HTTP %s: %s", resp.status, body[:200])
            return False
    except urllib.error.URLError as exc:
        log.error("CallMeBot network error: %s", exc)
        return False


def send_twilio_message(message: str, whatsapp: bool = False) -> bool:
    """Send SMS or WhatsApp message via Twilio REST API."""
    if "XXXX" in TWILIO_ACCOUNT_SID or "YOUR_" in TWILIO_AUTH_TOKEN:
        log.error("Twilio is not configured. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, TWILIO_TO.")
        return False

    from_num = TWILIO_FROM.strip()
    to_num = TWILIO_TO.strip()
    if whatsapp:
        if not from_num.startswith("whatsapp:"):
            from_num = f"whatsapp:{from_num}"
        if not to_num.startswith("whatsapp:"):
            to_num = f"whatsapp:{to_num}"

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    data = urllib.parse.urlencode({
        "From": from_num,
        "To": to_num,
        "Body": message,
    }).encode("utf-8")

    token = base64.b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if 200 <= resp.status < 300:
                log.info("Twilio %s alert sent successfully.", "WhatsApp" if whatsapp else "SMS")
                return True
            log.error("Twilio returned HTTP %s", resp.status)
            return False
    except urllib.error.URLError as exc:
        log.error("Twilio network error: %s", exc)
        return False


def send_telegram_alert(message: str) -> bool:
    """Send alert via Telegram bot (instant, free, 100% reliable)."""
    token = TELEGRAM_BOT_TOKEN.strip()
    chat_id = TELEGRAM_CHAT_ID.strip()
    if not token or not chat_id:
        log.error("Telegram is not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if 200 <= resp.status < 300:
                log.info("Telegram alert sent successfully.")
                return True
            log.error("Telegram returned HTTP %s", resp.status)
            return False
    except urllib.error.URLError as exc:
        log.error("Telegram network error: %s", exc)
        return False


def send_ntfy_alert(message: str) -> bool:
    """Send push notification to phone via ntfy.sh (Zero sign-up required)."""
    topic = NTFY_TOPIC.strip()
    if not topic:
        log.error("ntfy is not configured. Set NTFY_TOPIC.")
        return False
    url = f"https://ntfy.sh/{topic}"
    headers = {
        "Title": "Avengers Doomsday Tickets OPEN on Prasad PCX!",
        "Priority": "urgent",
        "Tags": "rotating_light,ticket"
    }
    req = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if 200 <= resp.status < 300:
                log.info("ntfy push notification sent successfully.")
                return True
            return False
    except urllib.error.URLError as exc:
        log.error("ntfy error: %s", exc)
        return False


def send_alert(message: str) -> bool:
    play_desktop_alert()
    print("\n" + "=" * 72)
    print(message)
    print("=" * 72 + "\n")

    mode = ALERT_MODE.lower().strip()
    if mode == "callmebot":
        return send_callmebot_whatsapp(message)
    if mode == "telegram":
        return send_telegram_alert(message)
    if mode == "ntfy":
        return send_ntfy_alert(message)
    if mode == "twilio_sms":
        return send_twilio_message(message, whatsapp=False)
    if mode == "twilio_whatsapp":
        return send_twilio_message(message, whatsapp=True)
    if mode == "console_only":
        return True

    log.error("Unknown ALERT_MODE '%s'. Falling back to console only.", ALERT_MODE)
    return True


# ==============================================================================
# BOOKMYSHOW PARSING (DUAL FETCHER: curl_cffi + PLAYWRIGHT)
# ==============================================================================

def extract_initial_state(html_text: str) -> Optional[dict]:
    """Extract and parse window.__INITIAL_STATE__ from BookMyShow HTML."""
    if not HAS_BS4:
        return None
    soup = BeautifulSoup(html_text, "html.parser")
    for s in soup.find_all("script"):
        txt = s.text or ""
        if "window.__INITIAL_STATE__ = " in txt:
            idx = txt.find("window.__INITIAL_STATE__ = ")
            raw = txt[idx + len("window.__INITIAL_STATE__ = "):].strip()
            if raw.endswith(";"):
                raw = raw[:-1].strip()
            try:
                return json.loads(raw)
            except Exception:
                continue
    return None


def check_date_via_cffi(date_code: str) -> Tuple[bool, List[str], str]:
    """
    High-reliability fetcher using curl_cffi:
    Bypasses Cloudflare Turnstile bot detection by impersonating Chrome TLS/HTTP2.
    """
    if not HAS_CURL_CFFI:
        return False, [], ""

    url = PRASADS_BASE_URL.format(date=date_code)
    try:
        session = cffi_requests.Session()
        r = session.get(url, impersonate="chrome120", timeout=15)
        if r.status_code != 200:
            return False, [], url

        state = extract_initial_state(r.text)
        if state:
            api = state.get("venueShowtimesFunctionalApi", {})
            queries = api.get("queries", {})

            # 1. Check if the date is disabled or not present in ShowDatesArray
            date_disabled = True
            for qk, qv in queries.items():
                if "getShowtimesByVenue" in qk:
                    d = qv.get("data", {})
                    dates_arr = d.get("ShowDatesArray", [])
                    for dt_obj in dates_arr:
                        if dt_obj.get("DateCode") == date_code:
                            date_disabled = dt_obj.get("isDisabled", True)
                            break

            # If date is explicitly disabled on BMS or not yet in calendar, it's not open
            if date_disabled:
                return False, [], url

            # 2. Check shows under query for this specific date
            target_query = f"getShowtimesByVenue-PRHN-{date_code}"
            if target_query in queries:
                q_data = queries[target_query].get("data", {})
                events = q_data.get("showDetailsTransformed", {}).get("Event", [])
                matched_times = []
                for ev in events:
                    title = ev.get("EventTitle", "")
                    if any(k in title.lower() for k in MOVIE_KEYWORDS):
                        for ch in ev.get("ChildEvents", []):
                            name = ch.get("EventName", "")
                            lang = ch.get("EventLanguage", "")
                            if LANGUAGE_KEYWORD in name.lower() or LANGUAGE_KEYWORD in lang.lower():
                                for sh in ch.get("ShowTimes", []):
                                    attr = (sh.get("Attributes") or "").lower()
                                    if any(k in attr for k in SCREEN_KEYWORDS):
                                        t = sh.get("ShowTimeDisplay") or sh.get("ShowDateTime") or "Open"
                                        matched_times.append(t)
                if matched_times:
                    return True, matched_times, url

                # If the date is unlocked on calendar
                return True, ["Date unlocked on BookMyShow"], url

        return False, [], url

    except Exception as exc:
        log.debug("cffi fetch failed for %s: %s", date_code, exc)
        return False, [], url


def check_date_via_playwright(page, date_code: str) -> Tuple[bool, List[str], str]:
    """
    Playwright browser fetcher fallback.
    """
    url = PRASADS_BASE_URL.format(date=date_code)
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4500)

    final_url = page.url
    # Guard against BookMyShow silently redirecting an unopened date to today's date
    if date_code not in final_url:
        log.info("Date %s redirected to %s (not open yet).", date_code, final_url)
        return False, [], url

    # Check for Cloudflare challenge in title/content
    title = page.title()
    if "just a moment" in title.lower():
        log.warning("Playwright encountered Cloudflare challenge. Falling back to curl_cffi.")
        return False, [], url

    js_extract = """
    () => {
      const blocks = [];
      const candidates = document.querySelectorAll('li, section, div');
      for (const el of candidates) {
        const txt = (el.innerText || '').trim();
        if (!txt || txt.length > 2500) continue;
        const low = txt.toLowerCase();
        if (low.includes('doomsday') && low.includes('pcx')) {
          blocks.push(txt);
        }
      }
      if (blocks.length > 0) return blocks;
      return [(document.body && document.body.innerText) || ''];
    }
    """
    blocks = page.evaluate(js_extract)
    blocks.sort(key=len)

    matched_times: List[str] = []
    for block in blocks:
        lower = block.lower()
        has_movie = any(k in lower for k in MOVIE_KEYWORDS)
        has_lang = LANGUAGE_KEYWORD in lower
        has_pcx = any(k in lower for k in SCREEN_KEYWORDS)
        if has_movie and has_lang and has_pcx:
            times = TIME_PATTERN.findall(block)
            if times:
                for t in times:
                    t_norm = re.sub(r"\s+", " ", t.strip().upper())
                    if t_norm not in matched_times:
                        matched_times.append(t_norm)
            else:
                matched_times.append("Showtimes live (check link)")

    return bool(matched_times), matched_times, final_url


def run_check_cycle(alerted_dates: Set[str]) -> None:
    remaining = [(code, label) for code, label in TARGET_DATES if code not in alerted_dates]
    if not remaining:
        log.info("All target dates (Dec 18–20) have already been alerted.")
        return

    # Check via curl_cffi first (Primary, unblocked by Cloudflare)
    if HAS_CURL_CFFI:
        for date_code, date_label in remaining:
            log.info("Checking Prasads PCX for %s (%s)...", date_label, date_code)
            is_open, showtimes, live_url = check_date_via_cffi(date_code)
            if is_open:
                times_str = ", ".join(showtimes)
                msg = (
                    f"🚨 TICKETS OPEN! Avengers: Doomsday (English - PCX)\n"
                    f"Venue: Prasads Multiplex, Hyderabad\n"
                    f"Date: {date_label}\n"
                    f"Showtimes: {times_str}\n"
                    f"Book now: {live_url}"
                )
                log.info("OPEN DETECTED for %s! Sending alert...", date_label)
                if send_alert(msg):
                    alerted_dates.add(date_code)
                    save_alerted_dates(alerted_dates)
            else:
                log.info("Not open yet for %s.", date_label)
        return

    # Fallback to Playwright if curl_cffi is unavailable
    if HAS_PLAYWRIGHT:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=HEADLESS)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0.0.0 Safari/537.36"
                ),
                locale="en-IN",
                timezone_id="Asia/Kolkata",
            )
            page = context.new_page()

            try:
                for date_code, date_label in remaining:
                    log.info("Checking Prasads PCX for %s (%s) via Playwright...", date_label, date_code)
                    try:
                        is_open, showtimes, live_url = check_date_via_playwright(page, date_code)
                    except Exception as exc:
                        log.warning("Check failed for %s: %s", date_label, exc)
                        continue

                    if is_open:
                        times_str = ", ".join(showtimes)
                        msg = (
                            f"🚨 TICKETS OPEN! Avengers: Doomsday (English - PCX)\n"
                            f"Venue: Prasads Multiplex, Hyderabad\n"
                            f"Date: {date_label}\n"
                            f"Showtimes: {times_str}\n"
                            f"Book now: {live_url}"
                        )
                        log.info("OPEN DETECTED for %s! Sending alert...", date_label)
                        if send_alert(msg):
                            alerted_dates.add(date_code)
                            save_alerted_dates(alerted_dates)
                    else:
                        log.info("Not open yet for %s.", date_label)
            finally:
                context.close()
                browser.close()


# ==============================================================================
# MAIN LOOP
# ==============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Desktop bot to watch BookMyShow for Avengers: Doomsday PCX tickets."
    )
    parser.add_argument(
        "--test-alert",
        action="store_true",
        help="Send a test WhatsApp/SMS message and exit.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check cycle and exit.",
    )
    args = parser.parse_args()

    if args.test_alert:
        ok = send_alert(
            "Test alert from your Prasads PCX Ticket Bot:\n"
            "WhatsApp/SMS notifications are working!"
        )
        sys.exit(0 if ok else 1)

    log.info(
        "Starting Prasads PCX Ticket Bot (Dec 18–20, 2026 | English | Mode: %s | Interval: %ss)",
        ALERT_MODE,
        CHECK_INTERVAL_SECONDS,
    )
    alerted_dates = load_alerted_dates()
    if alerted_dates:
        log.info("Already alerted dates loaded from state: %s", sorted(alerted_dates))

    while True:
        # Stop automatically after Dec 20, 2026
        if datetime.now() > datetime(2026, 12, 20, 23, 59, 59):
            log.info("Target date window (Dec 18–20, 2026) has passed. Exiting.")
            break

        if len(alerted_dates) >= len(TARGET_DATES):
            log.info("Alerts sent for all target dates (Dec 18–20). Exiting.")
            break

        if not has_internet_connection():
            log.warning(
                "Internet disconnected. Waiting %ss before retrying...",
                OFFLINE_RETRY_SECONDS,
            )
            time.sleep(OFFLINE_RETRY_SECONDS)
            continue

        try:
            run_check_cycle(alerted_dates)
        except Exception as exc:
            log.error("Unexpected error in check cycle: %s", exc)

        if args.once:
            break

        log.info("Sleeping %ss until next check...", CHECK_INTERVAL_SECONDS)
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
BookMyShow Ticket Watcher — Prasads Multiplex, Hyderabad
--------------------------------------------------------
Monitors Avengers: Doomsday (English) across Dec 17–20, 2026 on BookMyShow.

Key fixes in this version:
  1. Per-showtime/screen tracking (alerted_shows.json) so standard or
     MS-Infinity Vision 3D shows NEVER mute a later PCX Screen 6 opening.
  2. Dual fetch engine: fast curl_cffi TLS impersonation with automatic
     Playwright Chromium fallback if BookMyShow returns a Cloudflare 403.
  3. Loud WARNING logs on HTTP 403 / Cloudflare blocks (no silent failures).
  4. Zero hardcoded secrets — reads Telegram/WhatsApp/SMS keys from
     environment variables (GitHub Secrets) or a local untracked config.json.
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

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ==============================================================================
# CONFIGURATION (Env vars / GitHub Secrets take priority over local config.json)
# ==============================================================================

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "alerted_shows.json"


def load_local_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


FILE_CONFIG = load_local_config()


def cfg(env_key: str, json_key: str, default: str = "") -> str:
    val = os.getenv(env_key)
    if val is not None and val.strip() != "":
        return val.strip()
    raw = FILE_CONFIG.get(json_key, default)
    return str(raw).strip() if raw is not None else default


# Alert channel: "telegram", "callmebot", "twilio_sms", "twilio_whatsapp", "ntfy", "console_only"
ALERT_MODE = cfg("BMS_ALERT_MODE", "alert_mode", "telegram").lower()

# Credentials — NEVER hardcode tokens here if pushing to a public GitHub repo
TELEGRAM_BOT_TOKEN = cfg("TELEGRAM_BOT_TOKEN", "telegram_bot_token")
TELEGRAM_CHAT_ID = cfg("TELEGRAM_CHAT_ID", "telegram_chat_id")
CALLMEBOT_PHONE = cfg("CALLMEBOT_PHONE", "callmebot_phone")
CALLMEBOT_API_KEY = cfg("CALLMEBOT_API_KEY", "callmebot_api_key")
NTFY_TOPIC = cfg("NTFY_TOPIC", "ntfy_topic")
TWILIO_ACCOUNT_SID = cfg("TWILIO_ACCOUNT_SID", "twilio_account_sid")
TWILIO_AUTH_TOKEN = cfg("TWILIO_AUTH_TOKEN", "twilio_auth_token")
TWILIO_FROM = cfg("TWILIO_FROM", "twilio_from")
TWILIO_TO = cfg("TWILIO_TO", "twilio_to")

# Target movie, venue, language, and screens
VENUE_CODE = cfg("BMS_VENUE_CODE", "venue_code", "PRHN")
MOVIE_KEYWORDS = ["avengers: doomsday", "avengers doomsday", "doomsday"]
LANGUAGE_KEYWORD = cfg("BMS_LANGUAGE", "language", "english").lower()
SCREEN_KEYWORDS = [
    "pcx screen",
    "pcx hdr",
    "pcx",
    "infinity vision",
    "infinity vision 3d",
    "ms infinity vision",
    "infinity",
    "screen 6",
    "screen-6",
    "scr 6",
]

# False = alert for ANY new showtime (PCX tagged prominently); True = PCX only
PCX_ONLY = cfg("BMS_PCX_ONLY", "pcx_only", "false").lower() == "true"

DEFAULT_DATES = [
    ("20261217", "Thu, 17 Dec 2026"),
    ("20261218", "Fri, 18 Dec 2026"),
    ("20261219", "Sat, 19 Dec 2026"),
    ("20261220", "Sun, 20 Dec 2026"),
]


def load_target_dates() -> List[Tuple[str, str]]:
    raw_dates = FILE_CONFIG.get("target_dates")
    if isinstance(raw_dates, list) and raw_dates:
        parsed = []
        for code in raw_dates:
            code_str = str(code).strip()
            try:
                dt = datetime.strptime(code_str, "%Y%m%d")
                parsed.append((code_str, dt.strftime("%a, %d %b %Y")))
            except ValueError:
                continue
        if parsed:
            return parsed
    return DEFAULT_DATES


TARGET_DATES = load_target_dates()

PRASADS_BASE_URL = (
    "https://in.bookmyshow.com/cinemas/hyderabad/"
    f"prasads-multiplex-hyderabad/buytickets/{VENUE_CODE}/{{date}}"
)

CHECK_INTERVAL_SECONDS = int(cfg("BMS_CHECK_INTERVAL", "polling_interval_seconds", "45"))
OFFLINE_RETRY_SECONDS = 30
HEADLESS = cfg("BMS_HEADLESS", "headless", "true").lower() != "false"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pcx_ticket_bot")

TIME_PATTERN = re.compile(r"\b(?:0?[1-9]|1[0-2]):[0-5]\d\s?(?:AM|PM)\b", re.IGNORECASE)


# ==============================================================================
# NETWORK & PER-SHOWTIME STATE HELPERS
# ==============================================================================

def has_internet_connection(timeout: float = 5.0) -> bool:
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 443)):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def load_alerted_shows() -> Set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("alerted_shows", []))
    except Exception:
        return set()


def save_alerted_shows(shows: Set[str]) -> None:
    try:
        STATE_FILE.write_text(
            json.dumps({"alerted_shows": sorted(shows)}, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("Could not save state file %s: %s", STATE_FILE, exc)


def format_show_time(raw_time: str) -> str:
    """Format raw time strings like '202612170800' into '08:00 AM'."""
    raw = raw_time.strip()
    if len(raw) == 12 and raw.isdigit():
        try:
            hh = int(raw[8:10])
            mm = int(raw[10:12])
            period = "AM" if hh < 12 else "PM"
            hh_12 = hh if 1 <= hh <= 12 else (hh - 12 if hh > 12 else 12)
            return f"{hh_12:02d}:{mm:02d} {period}"
        except Exception:
            pass
    return raw.upper()


def make_show_key(date_code: str, show_time: str, screen_label: str, is_main: bool = False) -> str:
    clean_time = format_show_time(show_time)
    if is_main:
        clean_screen = "MAIN_SCREEN"
    else:
        clean_screen = re.sub(r"\s+", " ", screen_label.strip().upper())
    return f"{date_code}|{clean_time}|{clean_screen}"


def git_commit_and_push_state() -> None:
    """Immediately commit and push alerted_shows.json in GitHub Actions so other runs never miss it."""
    try:
        import subprocess
        if not os.getenv("GITHUB_ACTIONS"):
            return
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False)
        subprocess.run(["git", "add", "alerted_shows.json"], check=False)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", "chore: auto-save alerted shows [skip ci]"], check=False)
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
            subprocess.run(["git", "push", "origin", "main"], check=False)
            log.info("alerted_shows.json successfully committed and pushed to GitHub.")
    except Exception as exc:
        log.warning("Could not auto-push alerted_shows.json: %s", exc)


# ==============================================================================
# ALERTING
# ==============================================================================

def play_desktop_alert() -> None:
    try:
        if sys.platform.startswith("win"):
            import winsound  # type: ignore
            for _ in range(4):
                winsound.Beep(1200, 280)
                winsound.Beep(1800, 280)
                time.sleep(0.1)
        else:
            for _ in range(3):
                sys.stdout.write("\a")
                sys.stdout.flush()
                time.sleep(0.25)
    except Exception:
        pass


def send_telegram_alert(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": message}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
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


def send_callmebot_whatsapp(message: str) -> bool:
    phone = CALLMEBOT_PHONE.replace("+", "")
    if not phone or "XXXX" in phone or not CALLMEBOT_API_KEY:
        log.error("CallMeBot not configured. Set CALLMEBOT_PHONE and CALLMEBOT_API_KEY.")
        return False
    query = urllib.parse.urlencode({"phone": phone, "text": message, "apikey": CALLMEBOT_API_KEY})
    url = f"https://api.callmebot.com/whatsapp.php?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "PCXTicketBot/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except urllib.error.URLError as exc:
        log.error("CallMeBot network error: %s", exc)
        return False


def send_ntfy_alert(message: str, click_url: str = "") -> bool:
    if not NTFY_TOPIC:
        log.error("ntfy not configured. Set NTFY_TOPIC.")
        return False
    headers = {"Title": "Prasads Ticket Alert - Avengers Doomsday", "Priority": "urgent"}
    if click_url:
        headers["Click"] = click_url
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except urllib.error.URLError as exc:
        log.error("ntfy network error: %s", exc)
        return False


def send_twilio_message(message: str, whatsapp: bool = False) -> bool:
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_FROM or not TWILIO_TO:
        log.error("Twilio not configured. Set TWILIO_* environment variables.")
        return False
    from_num = f"whatsapp:{TWILIO_FROM}" if whatsapp and not TWILIO_FROM.startswith("whatsapp:") else TWILIO_FROM
    to_num = f"whatsapp:{TWILIO_TO}" if whatsapp and not TWILIO_TO.startswith("whatsapp:") else TWILIO_TO
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    data = urllib.parse.urlencode({"From": from_num, "To": to_num, "Body": message}).encode("utf-8")
    token = base64.b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except urllib.error.URLError as exc:
        log.error("Twilio network error: %s", exc)
        return False


def send_alert(message: str, click_url: str = "") -> bool:
    play_desktop_alert()
    print("\n" + "=" * 72)
    print(message)
    print("=" * 72 + "\n")

    if ALERT_MODE == "telegram":
        return send_telegram_alert(message)
    if ALERT_MODE == "callmebot":
        return send_callmebot_whatsapp(message)
    if ALERT_MODE == "ntfy":
        return send_ntfy_alert(message, click_url)
    if ALERT_MODE == "twilio_sms":
        return send_twilio_message(message, whatsapp=False)
    if ALERT_MODE == "twilio_whatsapp":
        return send_twilio_message(message, whatsapp=True)
    if ALERT_MODE == "console_only":
        return True
    log.error("Unknown ALERT_MODE '%s'.", ALERT_MODE)
    return False


# ==============================================================================
# BOOKMYSHOW PARSING (CURL_CFFI PRIMARY + PLAYWRIGHT FALLBACK)
# ==============================================================================

def extract_initial_state(html_text: str) -> Optional[dict]:
    if not HAS_BS4:
        return None
    soup = BeautifulSoup(html_text, "html.parser")
    for s in soup.find_all("script"):
        txt = s.text or ""
        marker = "window.__INITIAL_STATE__ = "
        idx = txt.find(marker)
        if idx != -1:
            raw = txt[idx + len(marker):].strip().rstrip(";").strip()
            try:
                return json.loads(raw)
            except Exception:
                continue
    return None


def parse_shows_from_initial_state(state: dict, date_code: str) -> List[Dict[str, object]]:
    """
    Extract matching English shows for Avengers: Doomsday from BookMyShow's
    venueShowtimesFunctionalApi payload. Returns a list of dicts:
      {"time": "08:00 AM", "screen": "PCX SCREEN", "is_pcx": True}
    """
    api = state.get("venueShowtimesFunctionalApi", {})
    queries = api.get("queries", {})
    target_query = f"getShowtimesByVenue-{VENUE_CODE}-{date_code}"

    q_data = queries.get(target_query, {}).get("data", {})
    if not q_data:
        return []

    events = q_data.get("showDetailsTransformed", {}).get("Event", [])
    results: List[Dict[str, object]] = []

    for ev in events:
        title = (ev.get("EventTitle") or "").lower()
        if not any(k in title for k in MOVIE_KEYWORDS):
            continue
        for ch in ev.get("ChildEvents", []):
            name = (ch.get("EventName") or "").lower()
            lang = (ch.get("EventLanguage") or "").lower()
            if LANGUAGE_KEYWORD and (LANGUAGE_KEYWORD not in name and LANGUAGE_KEYWORD not in lang):
                continue
            for sh in ch.get("ShowTimes", []):
                raw_attr = (sh.get("Attributes") or sh.get("ScreenName") or "Standard").strip()
                attr_low = raw_attr.lower()
                is_pcx = any(k in attr_low for k in SCREEN_KEYWORDS)
                if PCX_ONLY and not is_pcx:
                    continue
                show_time = (sh.get("ShowTimeDisplay") or sh.get("ShowDateTime") or "Open").strip()
                results.append({
                    "time": show_time,
                    "screen": raw_attr,
                    "is_pcx": is_pcx,
                })
    return results


def fetch_via_cffi(url: str, date_code: str) -> Tuple[str, List[Dict[str, object]]]:
    """Returns (status, shows) where status is 'ok', 'blocked', or 'error'."""
    if not HAS_CURL_CFFI:
        return "error", []
    try:
        session = cffi_requests.Session()
        r = session.get(url, impersonate="chrome120", timeout=15)
        if r.status_code in (403, 429, 503):
            log.warning("BookMyShow returned HTTP %s (possible Cloudflare block) on %s", r.status_code, date_code)
            return "blocked", []
        if r.status_code != 200:
            log.warning("BookMyShow returned HTTP %s for %s", r.status_code, date_code)
            return "error", []
        if "just a moment" in r.text[:2000].lower() or "attention required" in r.text[:2000].lower():
            log.warning("Cloudflare challenge page detected on %s", date_code)
            return "blocked", []
        state = extract_initial_state(r.text)
        if not state:
            log.warning("Could not parse __INITIAL_STATE__ for %s (page layout or block).", date_code)
            return "blocked", []
        return "ok", parse_shows_from_initial_state(state, date_code)
    except Exception as exc:
        log.warning("curl_cffi request failed for %s: %s", date_code, exc)
        return "error", []


def fetch_via_playwright(url: str, date_code: str) -> List[Dict[str, object]]:
    """Fallback browser check if curl_cffi is blocked by Cloudflare."""
    if not HAS_PLAYWRIGHT:
        log.warning("Playwright not installed; cannot run browser fallback.")
        return []
    shows: List[Dict[str, object]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4500)
            if date_code not in page.url:
                return []
            title = (page.title() or "").lower()
            if "just a moment" in title or "attention required" in title:
                log.warning("Playwright also hit Cloudflare challenge on %s.", date_code)
                return []
            html = page.content()
            state = extract_initial_state(html)
            if state:
                return parse_shows_from_initial_state(state, date_code)

            # DOM text fallback if __INITIAL_STATE__ wasn't embedded
            body_text = page.inner_text("body")
            low = body_text.lower()
            if any(k in low for k in MOVIE_KEYWORDS) and LANGUAGE_KEYWORD in low:
                is_pcx = any(k in low for k in SCREEN_KEYWORDS)
                if not PCX_ONLY or is_pcx:
                    times = sorted(set(TIME_PATTERN.findall(body_text))) or ["Check BookMyShow"]
                    for t in times:
                        shows.append({
                            "time": t.upper(),
                            "screen": "PCX Screen" if is_pcx else "Prasads Screen",
                            "is_pcx": is_pcx,
                        })
        finally:
            context.close()
            browser.close()
    return shows


def filter_new_shows(
    date_code: str, shows: List[Dict[str, object]], alerted_shows: Set[str]
) -> List[Tuple[str, Dict[str, object]]]:
    """Return only (key, show_dict) entries not previously alerted."""
    new_items: List[Tuple[str, Dict[str, object]]] = []
    for sh in shows:
        time_str = str(sh["time"])
        screen_str = str(sh["screen"])
        is_pcx = bool(sh.get("is_pcx", False))

        norm_key = make_show_key(date_code, time_str, screen_str, is_main=is_pcx)
        raw_key = f"{date_code}|{time_str.upper()}|{screen_str.upper()}"

        if norm_key in alerted_shows or raw_key in alerted_shows:
            continue
        new_items.append((norm_key, sh))
    return new_items


def run_check_cycle(alerted_shows: Set[str]) -> bool:
    found_new = False
    for date_code, date_label in TARGET_DATES:
        url = PRASADS_BASE_URL.format(date=date_code)
        log.info("Checking Prasads for %s (%s)...", date_label, date_code)

        status, shows = fetch_via_cffi(url, date_code)
        if status == "blocked":
            log.info("Falling back to Playwright for %s...", date_label)
            shows = fetch_via_playwright(url, date_code)

        new_items = filter_new_shows(date_code, shows, alerted_shows)
        if not new_items:
            log.info("No new shows for %s (total visible: %d).", date_label, len(shows))
            continue

        found_new = True
        has_pcx = any(bool(sh["is_pcx"]) for _, sh in new_items)
        header = "🚨 [PCX / INFINITY VISION (MAIN SCREEN) OPEN!] 🚨" if has_pcx else "🎟️ NEW PRASADS SHOWS OPEN!"
        lines = []
        for _, sh in new_items:
            display_time = format_show_time(str(sh["time"]))
            tag = "[PCX / INFINITY VISION (SCREEN 6)]" if sh["is_pcx"] else f"[{sh['screen']}]"
            lines.append(f"  • {display_time} {tag}")

        msg = (
            f"{header}\n\n"
            f"🎬 Movie: Avengers: Doomsday (English)\n"
            f"📍 Venue: Prasads Multiplex, Hyderabad\n"
            f"📅 Date: {date_label}\n"
            f"⏰ New Shows:\n" + "\n".join(lines) + f"\n\n🔗 Book now:\n{url}"
        )
        log.info("NEW SHOWS DETECTED for %s (%d new)! Sending alert...", date_label, len(new_items))
        if send_alert(msg, click_url=url):
            for key, sh in new_items:
                alerted_shows.add(key)
                raw_k = f"{date_code}|{str(sh['time']).upper()}|{str(sh['screen']).upper()}"
                alerted_shows.add(raw_k)
            save_alerted_shows(alerted_shows)
            git_commit_and_push_state()
        else:
            log.warning("Alert delivery failed for %s; will retry next cycle.", date_label)

    return found_new


# ==============================================================================
# MAIN LOOP
# ==============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Prasads Multiplex BookMyShow Ticket Watcher")
    parser.add_argument("--test-alert", action="store_true", help="Send a test alert and exit.")
    parser.add_argument("--once", action="store_true", help="Run a single check cycle and exit.")
    parser.add_argument("--loop-minutes", type=int, default=0, help="Run continuous loop for N minutes.")
    args = parser.parse_args()

    if args.test_alert:
        ok = send_alert("🎬 Test alert from Prasads Ticket Bot: Notifications are working!")
        sys.exit(0 if ok else 1)

    log.info(
        "Starting Prasads Watcher (Mode: %s | PCX Only: %s | Dates: %s | Interval: %ss)",
        ALERT_MODE,
        PCX_ONLY,
        [d[0] for d in TARGET_DATES],
        CHECK_INTERVAL_SECONDS,
    )
    alerted_shows = load_alerted_shows()

    if args.once:
        run_check_cycle(alerted_shows)
        return

    start_time = time.time()
    max_duration = args.loop_minutes * 60 if args.loop_minutes > 0 else 0

    while True:
        if datetime.now() > datetime(2026, 12, 20, 23, 59, 59):
            log.info("Target date window passed. Exiting.")
            break

        if not has_internet_connection():
            log.warning("Internet disconnected. Retrying in %ss...", OFFLINE_RETRY_SECONDS)
            time.sleep(OFFLINE_RETRY_SECONDS)
            continue

        try:
            run_check_cycle(alerted_shows)
        except Exception as exc:
            log.error("Unexpected error in check cycle: %s", exc)

        if max_duration > 0 and (time.time() - start_time) >= max_duration:
            log.info("Completed %s-minute monitoring window.", args.loop_minutes)
            break

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

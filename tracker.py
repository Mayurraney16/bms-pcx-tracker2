"""
BookMyShow Ticket Tracker for Prasads PCX Screen
Movie: Avengers: Doomsday (English)
Dates: Dec 18, 19, 20
"""

import sys
import os
import json
import time
import random
import threading
import webbrowser
from datetime import datetime
from bs4 import BeautifulSoup
from curl_cffi import requests

# Windows sound & alert support
try:
    import winsound
except ImportError:
    winsound = None

try:
    import ctypes
except ImportError:
    ctypes = None


DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config():
    if os.path.exists(DEFAULT_CONFIG_PATH):
        try:
            with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[!] Warning: Failed to load config.json: {e}")
    return {}


CONFIG = load_config()

CITY = CONFIG.get("city", "hyderabad")
REGION_CODE = CONFIG.get("region_code", "HYD")
VENUE_CODE = CONFIG.get("venue_code", "PRHN")
VENUE_NAME = CONFIG.get("venue_name", "Prasads Multiplex: Hyderabad")
MOVIE_TITLE = CONFIG.get("movie_title", "Avengers: Doomsday")
MOVIE_CODES = CONFIG.get("movie_codes", ["ET00439706", "ET00526306"])
TARGET_DATES = CONFIG.get("target_dates", ["20261218", "20261219", "20261220"])
LANGUAGE = CONFIG.get("language", "English")
PCX_ONLY = CONFIG.get("pcx_screen_only", True)
POLL_INTERVAL = CONFIG.get("polling_interval_seconds", 45)

ENABLE_SOUND = CONFIG.get("enable_sound_alarm", True)
ENABLE_POPUP = CONFIG.get("enable_desktop_popup", True)
ENABLE_BROWSER = CONFIG.get("enable_auto_open_browser", True)

NTFY_TOPIC = CONFIG.get("ntfy_topic", "")
TELEGRAM_TOKEN = CONFIG.get("telegram_bot_token", "")
TELEGRAM_CHAT_ID = CONFIG.get("telegram_chat_id", "")
DISCORD_WEBHOOK = CONFIG.get("discord_webhook_url", "")


def play_alarm():
    """Play repeating alert sound in background thread."""
    if not winsound:
        return
    for _ in range(8):
        try:
            winsound.Beep(1200, 300)
            winsound.Beep(1800, 300)
            winsound.Beep(2400, 400)
            time.sleep(0.1)
        except Exception:
            break


def show_desktop_popup(title, message):
    """Show modal Windows alert box."""
    if ctypes:
        try:
            # MB_OK | MB_ICONEXCLAMATION | MB_SYSTEMMODAL (0x00001000)
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x30 | 0x1000)
        except Exception as e:
            print(f"[!] Failed to display popup: {e}")


def send_ntfy_alert(title, message, booking_url):
    """Send push notification to phone via ntfy.sh (No account needed)."""
    if not NTFY_TOPIC:
        return
    try:
        url = f"https://ntfy.sh/{NTFY_TOPIC.strip()}"
        # Header values must be ASCII/Latin-1
        clean_title = "Avengers Doomsday Tickets OPEN on Prasad PCX!"
        headers = {
            "Title": clean_title,
            "Priority": "urgent",
            "Tags": "rotating_light,ticket,movie",
            "Click": booking_url
        }
        requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=10)
        print(f"[+] Phone push notification sent via ntfy.sh topic: {NTFY_TOPIC}")
    except Exception as e:
        print(f"[!] ntfy error: {e}")


def send_telegram_alert(message):
    """Send alert via Telegram bot if configured."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=10)
        print("[+] Telegram alert sent successfully.")
    except Exception as e:
        print(f"[!] Telegram alert error: {e}")


def send_discord_alert(message):
    """Send alert via Discord webhook if configured."""
    if not DISCORD_WEBHOOK:
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": message}, timeout=10)
        print("[+] Discord alert sent successfully.")
    except Exception as e:
        print(f"[!] Discord alert error: {e}")


def notify_all(booking_url, show_details):
    """Trigger all configured alert mechanisms."""
    title = "🚨 BOOKING OPEN: Avengers Doomsday on Prasad PCX!"
    msg = (
        f"🚨 AVENGERS: DOOMSDAY TICKETS OPEN! 🚨\n\n"
        f"Cinema: {VENUE_NAME}\n"
        f"Screen: {show_details.get('screen', 'PCX Screen')}\n"
        f"Date: {show_details.get('date', 'Dec 18-20')}\n"
        f"Time: {show_details.get('time', 'Showtime')}\n"
        f"Language/Format: {show_details.get('format', 'English 3D')}\n\n"
        f"Book Now Link:\n{booking_url}\n"
    )

    print("\n" + "=" * 70)
    print(msg)
    print("=" * 70 + "\n")

    # 1. Play loud audio alarm
    if ENABLE_SOUND:
        threading.Thread(target=play_alarm, daemon=True).start()

    # 2. Open browser automatically
    if ENABLE_BROWSER and booking_url:
        try:
            print(f"[+] Opening browser to: {booking_url}")
            webbrowser.open(booking_url)
        except Exception as e:
            print(f"[!] Failed to auto-open browser: {e}")

    # 3. Mobile alerts
    send_ntfy_alert(title, msg, booking_url)
    send_telegram_alert(msg)
    send_discord_alert(msg)

    # 4. Windows Desktop popup
    if ENABLE_POPUP:
        threading.Thread(target=show_desktop_popup, args=(title, msg), daemon=True).start()


def extract_initial_state(html_text):
    """Extract and parse window.__INITIAL_STATE__ from BMS HTML."""
    soup = BeautifulSoup(html_text, "html.parser")
    for s in soup.find_all("script"):
        txt = s.text or ""
        if "window.__INITIAL_STATE__" in txt:
            try:
                raw = txt.strip().replace("window.__INITIAL_STATE__ = ", "", 1)
                return json.loads(raw)
            except Exception:
                continue
    return None


def check_venue_date(session, date_code):
    """
    Check Prasads Multiplex page for a specific date:
    https://in.bookmyshow.com/cinemas/hyderabad/prasads-multiplex-hyderabad/buytickets/PRHN/{date_code}
    """
    url = f"https://in.bookmyshow.com/cinemas/{CITY}/prasads-multiplex-{CITY}/buytickets/{VENUE_CODE}/{date_code}"
    try:
        r = session.get(url, impersonate="chrome120", timeout=12)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"

        state = extract_initial_state(r.text)
        if not state:
            return None, "Could not extract __INITIAL_STATE__"

        api = state.get("venueShowtimesFunctionalApi", {})
        queries = api.get("queries", {})

        # Check date in ShowDatesArray
        date_is_disabled = True
        show_dates_array = []

        for qk, qv in queries.items():
            if "getShowtimesByVenue" in qk:
                d = qv.get("data", {})
                show_dates_array = d.get("ShowDatesArray", [])
                for d_item in show_dates_array:
                    if d_item.get("DateCode") == date_code:
                        date_is_disabled = d_item.get("isDisabled", True)
                        break

        # Check events inside query matching date_code or active query
        target_query_key = f"getShowtimesByVenue-{VENUE_CODE}-{date_code}"
        query_data = queries.get(target_query_key, {}).get("data", {})

        # If the date-specific query is present and fulfilled
        if query_data:
            events = query_data.get("showDetailsTransformed", {}).get("Event", [])
            for ev in events:
                title = ev.get("EventTitle", "")
                if "avengers" in title.lower() or "doomsday" in title.lower():
                    child_events = ev.get("ChildEvents", [])
                    for ch in child_events:
                        event_name = ch.get("EventName", "")
                        event_lang = ch.get("EventLanguage", "")
                        
                        # Check language filter
                        is_lang_match = (
                            not LANGUAGE
                            or LANGUAGE.lower() in event_name.lower()
                            or LANGUAGE.lower() in event_lang.lower()
                        )
                        if not is_lang_match:
                            continue

                        for sh in ch.get("ShowTimes", []):
                            attr = sh.get("Attributes", "") or ""
                            screen_name = sh.get("ScreenName", "") or ""
                            
                            is_pcx = "pcx" in attr.lower() or "pcx" in screen_name.lower()
                            if PCX_ONLY and not is_pcx:
                                continue

                            # MATCH FOUND!
                            return {
                                "date": date_code,
                                "time": sh.get("ShowTimeDisplay", sh.get("ShowDateTime")),
                                "screen": attr if is_pcx else (screen_name or "Standard Screen"),
                                "format": ch.get("EventDimension", "3D"),
                                "event_name": event_name,
                                "url": url
                            }, "MATCH"

        # If date is not disabled in date array, booking has opened!
        if show_dates_array and not date_is_disabled:
            return {
                "date": date_code,
                "time": "Booking Just Opened!",
                "screen": "PCX Screen (Date Unlocked)",
                "format": "Check Shows",
                "event_name": "Avengers: Doomsday",
                "url": url
            }, "DATE_UNLOCKED"

        return None, f"Not opened yet (Date disabled: {date_is_disabled})"

    except Exception as e:
        return None, f"Error: {e}"


def check_movie_date(session, movie_code, date_code):
    """
    Check Avengers Doomsday movie-level page for Prasads:
    https://in.bookmyshow.com/buytickets/avengers-doomsday-hyderabad/movie-hyd-{movie_code}-MT/{date_code}
    """
    url = f"https://in.bookmyshow.com/buytickets/avengers-doomsday-{CITY}/movie-hyd-{movie_code}-MT/{date_code}"
    try:
        r = session.get(url, impersonate="chrome120", timeout=12)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"

        state = extract_initial_state(r.text)
        if not state:
            return None, "Could not extract __INITIAL_STATE__"

        queries = state.get("showtimesFunctionalApi", {}).get("queries", {})
        for qk, qv in queries.items():
            if "fetchPrimaryDynamic" in qk:
                inner_data = qv.get("data", {}).get("data", {})
                widgets = inner_data.get("showtimeWidgets", [])
                for w in widgets:
                    if w.get("type") == "groupList":
                        for grp in w.get("data", []):
                            if grp.get("type") == "venueGroup":
                                for vc in grp.get("data", []):
                                    ad = vc.get("additionalData", {})
                                    vcode = ad.get("venueCode")
                                    vname = ad.get("venueName", "")
                                    
                                    if vcode == VENUE_CODE or "prasad" in vname.lower():
                                        # PRHN found under this movie!
                                        sections = vc.get("showtimesSections", [])
                                        for sec in sections:
                                            for st in sec.get("showtimes", []):
                                                attr = st.get("screenAttr", "") or ""
                                                is_pcx = "pcx" in attr.lower()
                                                if PCX_ONLY and not is_pcx:
                                                    continue
                                                return {
                                                    "date": date_code,
                                                    "time": st.get("title", ""),
                                                    "screen": attr or "PCX Screen",
                                                    "format": "English 3D",
                                                    "event_name": MOVIE_TITLE,
                                                    "url": url
                                                }, "MATCH"

                                        # If PRHN is listed even without specific PCX parsed
                                        if not PCX_ONLY:
                                            return {
                                                "date": date_code,
                                                "time": "Shows Available",
                                                "screen": "Prasads Multiplex",
                                                "format": "English",
                                                "event_name": MOVIE_TITLE,
                                                "url": url
                                            }, "MATCH"

        return None, "Prasads not yet listed on movie page"

    except Exception as e:
        return None, f"Error: {e}"


def run_single_check(session):
    """Run one full pass across all target dates and detection methods."""
    for date_code in TARGET_DATES:
        # 1. Venue-centric check
        match, status = check_venue_date(session, date_code)
        if match:
            return match

        # 2. Movie-centric check
        for m_code in MOVIE_CODES:
            match, status2 = check_movie_date(session, m_code, date_code)
            if match:
                return match

    return None


def main():
    print("=" * 70)
    print("      🎬 BOOKMYSHOW PRASAD PCX TICKET TRACKER 🎬")
    print("=" * 70)
    print(f"Target Movie   : {MOVIE_TITLE} ({LANGUAGE})")
    print(f"Cinema Venue   : {VENUE_NAME} [{VENUE_CODE}]")
    print(f"Target Dates   : {', '.join(TARGET_DATES)}")
    print(f"PCX Screen Only: {'Yes' if PCX_ONLY else 'Any Screen'}")
    print(f"Poll Interval  : ~{POLL_INTERVAL} seconds")
    if NTFY_TOPIC:
        print(f"Phone Alerts   : Enabled (ntfy.sh/{NTFY_TOPIC})")
    if TELEGRAM_CHAT_ID:
        print("Telegram Alerts: Enabled")
    if DISCORD_WEBHOOK:
        print("Discord Alerts : Enabled")
    print("=" * 70)

    # Test mode flag
    if "--test" in sys.argv:
        print("\n[*] RUNNING IN TEST MODE...")
        test_url = f"https://in.bookmyshow.com/cinemas/{CITY}/prasads-multiplex-{CITY}/buytickets/{VENUE_CODE}/20261218"
        test_match = {
            "date": "20261218",
            "time": "07:30 PM",
            "screen": "PCX Screen (TEST)",
            "format": "English 3D",
            "event_name": "Avengers: Doomsday (TEST RUN)",
            "url": test_url
        }
        notify_all(test_url, test_match)
        print("[*] Test completed successfully.")
        return

    session = requests.Session()
    check_count = 0

    print("\n[*] Monitoring BookMyShow active... Press Ctrl+C to stop.")

    try:
        while True:
            check_count += 1
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{now_str}] Check #{check_count}: Scanning dates {TARGET_DATES} at Prasad PCX...", end="", flush=True)

            match = run_single_check(session)

            if match:
                print(" -> 🚨 TICKETS DETECTED! 🚨")
                booking_url = match.get("url")
                notify_all(booking_url, match)
                # Keep notifying every 2 minutes so user doesn't miss it
                time.sleep(120)
            else:
                print(" [Not open yet]")
                # Add jitter of ±4 seconds
                jitter = random.uniform(-4, 4)
                sleep_time = max(10, POLL_INTERVAL + jitter)
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("\n[*] Tracker stopped by user.")


if __name__ == "__main__":
    main()

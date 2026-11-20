# 🎬 BookMyShow Ticket Tracker for Prasad PCX Screen
### Movie: *Avengers: Doomsday* (English)
### Screen: Prasads Multiplex Large Screen (PCX), Hyderabad
### Target Dates: December 18, 19, and 20, 2026

This automated bot continuously monitors BookMyShow for **Prasads Multiplex (`PRHN`) in Hyderabad** specifically watching for when ticket bookings open for **Avengers: Doomsday** on the **PCX Screen** in **English** for December 18–20.

---

## ⚡ How It Works

1. **Bypasses Bot Protection**: Employs browser TLS fingerprint impersonation (`curl_cffi`) to query BookMyShow seamlessly without Cloudflare blocks or CAPTCHAs.
2. **Dual Detection Engine**:
   - **Venue-Centric Check**: Scans `https://in.bookmyshow.com/cinemas/hyderabad/prasads-multiplex-hyderabad/buytickets/PRHN/{date}` to inspect the live showtimes calendar and detects when Dec 18, 19, or 20 are unlocked and shows are scheduled.
   - **Movie-Centric Check**: Scans BookMyShow's movie pages for *Avengers: Doomsday* (`ET00439706` / `ET00526306`) in Hyderabad and verifies when Prasad's Multiplex is added to the active theater roster.
3. **PCX Screen & English Filter**:
   - Specifically filters for the **PCX Screen** (`Attributes` / `screenAttr` containing `PCX`).
   - Ensures language is **English** (2D/3D).

---

## 🚨 Instant Multi-Channel Alerts

The moment tickets are detected:
- 🔊 **Loud Audio Alarm**: Windows repeatedly rings an alert beep sequence (`winsound`) so you hear it even from another room.
- 🌐 **Auto-Open Booking Page**: Immediately pops open your default web browser (Chrome/Edge) straight to the BookMyShow seat booking page so you can grab tickets before they sell out.
- 💬 **Windows Desktop Popup**: Pushes a modal popup notification over any open window.
- 📱 **Instant Phone Push Notification (Free & Zero Account Required)**:
  - Uses `ntfy.sh`. Simply install the free **ntfy** app on your iPhone or Android (or open [ntfy.sh/prasad-avengers-tracker](https://ntfy.sh/prasad-avengers-tracker) in your phone browser) and subscribe to the topic `prasad-avengers-tracker`.
  - When tickets open, your phone will ring and vibrate instantly with a direct link to book!
- 🤖 **Telegram / Discord (Optional)**: Can optionally send instant messages to a Telegram bot or Discord channel.

---

## 🚀 Quick Start

### 1. Launch with One Click
Double-click `run_bot.bat` or run in terminal:
```powershell
python tracker.py
```

### 2. Test Your Alerts
To verify sound, browser auto-launch, and mobile alerts right now:
```powershell
python tracker.py --test
```

---

## ⚙️ Configuration (`config.json`)

You can customize all tracker options inside `config.json`:

```json
{
  "city": "hyderabad",
  "region_code": "HYD",
  "venue_code": "PRHN",
  "venue_name": "Prasads Multiplex: Hyderabad",
  "movie_title": "Avengers: Doomsday",
  "movie_codes": [
    "ET00439706",
    "ET00526306"
  ],
  "target_dates": [
    "20261218",
    "20261219",
    "20261220"
  ],
  "language": "English",
  "pcx_screen_only": true,
  "polling_interval_seconds": 45,
  "enable_sound_alarm": true,
  "enable_desktop_popup": true,
  "enable_auto_open_browser": true,
  "ntfy_topic": "prasad-avengers-tracker",
  "telegram_bot_token": "",
  "telegram_chat_id": "",
  "discord_webhook_url": ""
}
```

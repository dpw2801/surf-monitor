#!/usr/bin/env python3
"""
Surfline Conditions Monitor

Two modes running in the same loop:
  Real-time  — checks every 30 min, alerts when any spot hits GOOD (dark green)
               or better with qualifying wave/wind conditions.
  Daily forecast — runs once per day, looks 1–5 days ahead and sends a single
               summary notification for any days with waves >= threshold.
"""

import json
import logging
import math
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE = Path(__file__).parent
CONFIG_PATH = BASE / "config.json"
LOG_PATH = Path.home() / ".surf_monitor.log"
LAST_FORECAST_DATE_FILE = BASE / ".last_forecast_date"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH),
    ],
)
log = logging.getLogger(__name__)

# ── Config defaults ───────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "latitude": 21.3069,
    "longitude": -157.8583,
    "drive_radius_miles": 80,
    "check_interval_minutes": 30,
    "ntfy_topic": "surf-report-checke",
    # Real-time alert threshold (dark green on Surfline)
    "rating_threshold": "GOOD",
    # Real-time: minimum max wave height in feet
    "min_wave_height_ft": 2.5,
    # Real-time: max wind speed in mph (offshore overrides this)
    "max_wind_speed_mph": 15,
    # Forecast: minimum max wave height to include a day in the daily summary
    "forecast_min_wave_height_ft": 3.0,
    # Forecast: how many days ahead to scan (1 = tomorrow only, 5 = full 5-day)
    "forecast_days_ahead": 5,
    # How many hours after midnight to send the daily forecast (default: 7am)
    "forecast_hour": 7,
}

# Surfline rating scale (used for real-time only — forecasts lack machine ratings)
RATING_ORDER = [
    "FLAT", "VERY_POOR", "POOR", "POOR_TO_FAIR",
    "FAIR", "FAIR_TO_GOOD", "GOOD", "VERY_GOOD", "EPIC",
]
RATING_LABEL = {
    "FLAT": "Flat", "VERY_POOR": "Very Poor", "POOR": "Poor",
    "POOR_TO_FAIR": "Poor to Fair", "FAIR": "Fair",
    "FAIR_TO_GOOD": "Fair to Good", "GOOD": "Good",
    "VERY_GOOD": "Very Good", "EPIC": "EPIC",
}
RATING_EMOJI = {
    "FLAT": "⬜", "VERY_POOR": "🟥", "POOR": "🟥",
    "POOR_TO_FAIR": "🟨", "FAIR": "🟨",
    "FAIR_TO_GOOD": "🟩", "GOOD": "🟩", "VERY_GOOD": "🟩", "EPIC": "⭐",
}

SURFLINE = "https://services.surfline.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.surfline.com/",
}

KTS_TO_MPH = 1.15078


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    else:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        log.info(f"Default config written to {CONFIG_PATH}")
    return cfg


# ── Geo ───────────────────────────────────────────────────────────────────────

def haversine_miles(lat1, lon1, lat2, lon2) -> float:
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def bounding_box(lat, lon, radius_miles):
    deg_lat = radius_miles / 69.0
    deg_lon = radius_miles / (69.0 * math.cos(math.radians(lat)))
    return lat - deg_lat, lon - deg_lon, lat + deg_lat, lon + deg_lon


# ── Surfline API ──────────────────────────────────────────────────────────────

def _get(url, params) -> dict | None:
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.debug(f"GET {url} params={params} failed: {e}")
        return None


def fetch_all_spots(lat, lon, radius_miles) -> list[dict]:
    """Single mapview call — returns every spot with embedded real-time data."""
    south, west, north, east = bounding_box(lat, lon, radius_miles)
    data = _get(f"{SURFLINE}/kbyg/mapview",
                {"south": south, "west": west, "north": north, "east": east})
    if not data:
        return []
    spots = data["data"]["spots"]
    result = []
    for s in spots:
        d = haversine_miles(lat, lon, s.get("lat", 0), s.get("lon", 0))
        if d <= radius_miles:
            s["_dist"] = round(d, 1)
            result.append(s)
    log.info(f"Found {len(result)} spots within {radius_miles} mi")
    return result


def fetch_forecast(spot_id: str, days: int) -> tuple[list, list]:
    """Return (conditions_list, wave_list) for the given number of days."""
    cond = _get(f"{SURFLINE}/kbyg/spots/forecasts/conditions",
                {"spotId": spot_id, "days": days})
    wave = _get(f"{SURFLINE}/kbyg/spots/forecasts/wave",
                {"spotId": spot_id, "days": days, "intervalHours": 24})
    cond_list = (cond or {}).get("data", {}).get("conditions", [])
    wave_list = (wave or {}).get("data", {}).get("wave", [])
    return cond_list, wave_list


# ── Spot selection for forecast ───────────────────────────────────────────────

def select_forecast_spots(spots: list[dict], max_per_subregion: int = 2) -> list[dict]:
    """Pick the top-ranked spots per subregion to minimise forecast API calls."""
    by_subregion: dict[str, list] = {}
    for s in spots:
        key = s.get("subregionId", "unknown")
        by_subregion.setdefault(key, []).append(s)
    selected = []
    for group in by_subregion.values():
        ranked = sorted(group, key=lambda x: max(x.get("rank") or [0]), reverse=True)
        selected.extend(ranked[:max_per_subregion])
    return selected


# ── Condition helpers ─────────────────────────────────────────────────────────

def rating_index(r: str) -> int:
    try:
        return RATING_ORDER.index(r.upper().replace(" ", "_"))
    except ValueError:
        return -1


def wind_is_good(speed_kts: float, dir_type: str, max_mph: float) -> bool:
    dt = dir_type.lower()
    if "offshore" in dt or "calm" in dt:
        return True
    return (speed_kts * KTS_TO_MPH) <= max_mph


# ── Notifications ─────────────────────────────────────────────────────────────

def notify(topic: str, title: str, body: str, priority: str = "high") -> bool:
    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": "surfer,ocean_wave",
            },
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        log.error(f"ntfy push failed: {e}")
        return False


# ── Real-time check ───────────────────────────────────────────────────────────

def check_realtime(cfg: dict, spots: list[dict], alerted: set) -> None:
    threshold = rating_index(cfg["rating_threshold"])
    alerts_sent = 0

    for spot in spots:
        sid = spot.get("_id", "")
        name = spot.get("name", "Unknown")
        dist = spot.get("_dist", "?")

        # Rating
        rating = (spot.get("rating") or {}).get("key", "FLAT").upper().replace(" ", "_")
        if rating_index(rating) < threshold:
            log.debug(f"{name}: {rating} below threshold")
            continue

        # Wave height (surf.max from mapview is in feet)
        surf = spot.get("surf") or {}
        wave_min, wave_max = surf.get("min", 0.0), surf.get("max", 0.0)
        if wave_max < cfg["min_wave_height_ft"]:
            log.debug(f"{name}: waves {wave_max:.1f} ft below minimum")
            continue

        # Wind (speed in knots from Surfline)
        wind = spot.get("wind") or {}
        wind_kts = wind.get("speed", 999)
        wind_dir = wind.get("directionType", "")
        if not wind_is_good(wind_kts, wind_dir, cfg["max_wind_speed_mph"]):
            log.debug(f"{name}: wind {wind_kts:.0f} kts {wind_dir} not good")
            continue

        # Tide (informational)
        tide_current = (spot.get("tide") or {}).get("current") or {}
        tide_type = tide_current.get("type", "")
        tide_height = tide_current.get("height", "?")

        # De-duplicate per (spot, rating); re-alert on improvement
        key = f"{sid}:{rating}"
        if key in alerted:
            continue
        alerted -= {k for k in alerted if k.startswith(f"{sid}:")}
        alerted.add(key)

        emoji = RATING_EMOJI.get(rating, "🟩")
        label = RATING_LABEL.get(rating, rating)
        wind_mph = wind_kts * KTS_TO_MPH
        title = f"{emoji} {name} — {label}!"
        body = (
            f"Waves:    {wave_min:.1f}–{wave_max:.1f} ft  ({surf.get('humanRelation', '')})\n"
            f"Wind:     {wind_mph:.0f} mph {wind_dir}\n"
            f"Tide:     {tide_type}  ({tide_height} ft)\n"
            f"Distance: {dist} mi away"
        )
        log.info(f"REAL-TIME ALERT → {title}")
        if notify(cfg["ntfy_topic"], title, body):
            alerts_sent += 1
        else:
            log.error(f"Failed to push notification for {name}")

    if alerts_sent == 0:
        log.info("Real-time: no qualifying spots right now.")
    else:
        log.info(f"Real-time: sent {alerts_sent} alert(s).")


# ── Daily forecast check ──────────────────────────────────────────────────────

def should_run_forecast(cfg: dict) -> bool:
    """True once per day, after forecast_hour, until midnight."""
    now = datetime.now()
    if now.hour < cfg.get("forecast_hour", 7):
        return False
    today = now.strftime("%Y-%m-%d")
    if LAST_FORECAST_DATE_FILE.exists():
        if LAST_FORECAST_DATE_FILE.read_text().strip() == today:
            return False
    return True


def mark_forecast_sent() -> None:
    LAST_FORECAST_DATE_FILE.write_text(datetime.now().strftime("%Y-%m-%d"))


def check_forecast(cfg: dict, spots: list[dict]) -> None:
    log.info("Running daily forecast check...")
    forecast_spots = select_forecast_spots(spots, max_per_subregion=2)
    days = cfg.get("forecast_days_ahead", 5)
    min_ft = cfg.get("forecast_min_wave_height_ft", 3.0)

    # Aggregate: {date_key: {"label": str, "spots": [(name, wave_max, headline, dtw)]}}
    by_date: dict[str, dict] = {}

    for spot in forecast_spots:
        sid = spot.get("_id", "")
        name = spot.get("name", "Unknown")
        cond_list, wave_list = fetch_forecast(sid, days)

        # Build wave lookup by date
        wave_by_date: dict[str, dict] = {}
        for w in wave_list:
            ts = w.get("timestamp", 0)
            if not ts:
                continue
            dk = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            wave_by_date[dk] = w.get("surf", {})

        for day in cond_list:
            ts = day.get("timestamp", 0)
            if not ts:
                continue
            date_obj = datetime.fromtimestamp(ts)
            dk = date_obj.strftime("%Y-%m-%d")

            # Skip today — covered by real-time check
            if dk == datetime.now().strftime("%Y-%m-%d"):
                continue

            surf = wave_by_date.get(dk, {})
            wave_max = surf.get("max", 0)
            if wave_max < min_ft:
                continue

            wave_min = surf.get("min", 0)
            relation = surf.get("humanRelation", "")
            headline = (day.get("headline") or "").strip().rstrip(".")
            day_to_watch = day.get("dayToWatch", False)

            label = date_obj.strftime("%A, %b %-d")
            if dk not in by_date:
                by_date[dk] = {"label": label, "spots": []}
            by_date[dk]["spots"].append({
                "name": name,
                "wave_min": wave_min,
                "wave_max": wave_max,
                "relation": relation,
                "headline": headline,
                "day_to_watch": day_to_watch,
            })

        # Small delay to avoid hammering Surfline
        time.sleep(0.5)

    mark_forecast_sent()

    if not by_date:
        log.info("Forecast: no days with waves >= %.1f ft in the next %d days.", min_ft, days)
        notify(
            cfg["ntfy_topic"],
            "🌊 Surf Forecast — Nothing Notable",
            f"No days with waves ≥ {min_ft} ft in the next {days} days.",
            priority="low",
        )
        return

    # Build one summary notification
    lines = []
    for dk in sorted(by_date.keys()):
        info = by_date[dk]
        # Deduplicate headlines (multiple spots may share same regional forecast)
        seen_headlines: set[str] = set()
        day_to_watch = any(s["day_to_watch"] for s in info["spots"])
        dtw_flag = " ⭐ DAY TO WATCH" if day_to_watch else ""
        lines.append(f"📅 {info['label']}{dtw_flag}")

        for s in info["spots"]:
            lines.append(f"  • {s['name']}: {s['wave_min']:.0f}–{s['wave_max']:.0f} ft ({s['relation']})")
            if s["headline"] and s["headline"] not in seen_headlines:
                seen_headlines.add(s["headline"])
                lines.append(f"    \"{s['headline']}\"")

    body = "\n".join(lines)
    title = f"🌊 {len(by_date)}-Day Swell Outlook"
    log.info(f"FORECAST ALERT → {title}")
    notify(cfg["ntfy_topic"], title, body, priority="default")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    cfg = load_config()

    log.info(
        f"Surf Monitor starting | "
        f"center: ({cfg['latitude']}, {cfg['longitude']}) | "
        f"radius: {cfg['drive_radius_miles']} mi | "
        f"threshold: {cfg['rating_threshold']} | "
        f"interval: {cfg['check_interval_minutes']} min | "
        f"topic: {cfg['ntfy_topic']}"
    )

    notify(
        cfg["ntfy_topic"],
        "Surf Monitor Active",
        (
            f"Watching O'ahu spots within {cfg['drive_radius_miles']} mi.\n"
            f"Real-time alerts: rating ≥ {cfg['rating_threshold']}, "
            f"waves ≥ {cfg['min_wave_height_ft']} ft, good wind.\n"
            f"Daily forecast: waves ≥ {cfg['forecast_min_wave_height_ft']} ft "
            f"in next {cfg['forecast_days_ahead']} days, sent once at "
            f"{cfg['forecast_hour']}:00."
        ),
        priority="low",
    )

    alerted: set = set()
    while True:
        log.info("── Checking conditions ──")
        try:
            spots = fetch_all_spots(
                cfg["latitude"], cfg["longitude"], cfg["drive_radius_miles"]
            )
            if not spots:
                log.warning("No spots returned — internet issue or Surfline rate-limiting.")
            else:
                check_realtime(cfg, spots, alerted)
                if should_run_forecast(cfg):
                    check_forecast(cfg, spots)
        except Exception:
            log.exception("Unexpected error — will retry next interval.")

        wait = cfg["check_interval_minutes"] * 60
        log.info(f"Next check in {cfg['check_interval_minutes']} min.")
        time.sleep(wait)


if __name__ == "__main__":
    main()

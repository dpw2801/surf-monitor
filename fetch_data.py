#!/usr/bin/env python3
"""
Fetches current conditions + 7-day forecast from Surfline
and writes data/surf.json for the GitHub Pages dashboard.
"""

import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "config.json"
OUT_PATH = BASE / "data" / "surf.json"

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

RATING_ORDER = [
    "FLAT", "VERY_POOR", "POOR", "POOR_TO_FAIR",
    "FAIR", "FAIR_TO_GOOD", "GOOD", "VERY_GOOD", "EPIC",
]
RATING_LABEL = {
    "FLAT": "Flat", "VERY_POOR": "Very Poor", "POOR": "Poor",
    "POOR_TO_FAIR": "Poor to Fair", "FAIR": "Fair",
    "FAIR_TO_GOOD": "Fair to Good", "GOOD": "Good",
    "VERY_GOOD": "Very Good", "EPIC": "Epic",
}
RATING_CLASS = {
    "FLAT": "flat", "VERY_POOR": "poor", "POOR": "poor",
    "POOR_TO_FAIR": "fair", "FAIR": "fair",
    "FAIR_TO_GOOD": "fair", "GOOD": "good",
    "VERY_GOOD": "good", "EPIC": "epic",
}
WIND_BONUS = {
    "offshore": 2.0, "cross-offshore": 1.3,
    "onshore": 0.5, "cross-shore": 0.8, "cross-onshore": 0.6,
}

DEFAULT_CONFIG = {
    "latitude": 21.3069, "longitude": -157.8583,
    "drive_radius_miles": 80,
}


def _get(url, params):
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  WARN: GET {url} failed: {e}", file=sys.stderr)
        return None


def bounding_box(lat, lon, radius_miles):
    deg_lat = radius_miles / 69.0
    deg_lon = radius_miles / (69.0 * math.cos(math.radians(lat)))
    return lat - deg_lat, lon - deg_lon, lat + deg_lat, lon + deg_lon


def haversine_miles(lat1, lon1, lat2, lon2):
    R = 3958.8
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def fetch_current(cfg):
    lat, lon = cfg["latitude"], cfg["longitude"]
    s, w, n, e = bounding_box(lat, lon, cfg["drive_radius_miles"])
    data = _get(f"{SURFLINE}/kbyg/mapview",
                {"south": s, "west": w, "north": n, "east": e})
    if not data:
        return []

    spots = []
    for sp in data["data"]["spots"]:
        dist = haversine_miles(lat, lon, sp.get("lat", 0), sp.get("lon", 0))
        if dist > cfg["drive_radius_miles"]:
            continue
        rating_raw = (sp.get("rating") or {}).get("key", "FLAT").upper().replace(" ", "_")
        surf = sp.get("surf") or {}
        wind = sp.get("wind") or {}
        tide = (sp.get("tide") or {}).get("current") or {}
        spots.append({
            "id": sp.get("_id", ""),
            "name": sp.get("name", "?"),
            "lat": sp.get("lat", 0),
            "lon": sp.get("lon", 0),
            "dist": round(dist, 1),
            "rating": rating_raw,
            "ratingLabel": RATING_LABEL.get(rating_raw, rating_raw),
            "ratingClass": RATING_CLASS.get(rating_raw, "fair"),
            "waveMin": surf.get("min", 0),
            "waveMax": surf.get("max", 0),
            "waveRelation": surf.get("humanRelation", ""),
            "windMph": round(wind.get("speed", 0) * KTS_TO_MPH, 1),
            "windDir": wind.get("directionType", ""),
            "tideType": tide.get("type", ""),
            "tideHeight": tide.get("height", 0),
            "rank": max(sp.get("rank") or [0]),
        })
    return sorted(spots, key=lambda x: x["rank"], reverse=True)


def fetch_forecast(cfg, spots):
    lat, lon = cfg["latitude"], cfg["longitude"]
    today = datetime.now().strftime("%Y-%m-%d")

    # One top spot per subregion
    by_subregion = {}
    for sp in spots:
        # approximate subregion by latitude band
        band = round(sp["lat"] * 4) / 4
        by_subregion.setdefault(band, []).append(sp)
    rep = [sorted(v, key=lambda x: x["rank"], reverse=True)[0]
           for v in by_subregion.values()]

    best_per_spot = []

    for spot in rep:
        sid, name, dist = spot["id"], spot["name"], spot["dist"]
        wave_r = _get(f"{SURFLINE}/kbyg/spots/forecasts/wave",
                      {"spotId": sid, "days": 7, "intervalHours": 12})
        wind_r = _get(f"{SURFLINE}/kbyg/spots/forecasts/wind",
                      {"spotId": sid, "days": 7, "intervalHours": 12})
        cond_r = _get(f"{SURFLINE}/kbyg/spots/forecasts/conditions",
                      {"spotId": sid, "days": 7})
        tides_r = _get(f"{SURFLINE}/kbyg/spots/forecasts/tides",
                       {"spotId": sid, "days": 7})

        wave_data = (wave_r or {}).get("data", {}).get("wave", [])
        wind_data = (wind_r or {}).get("data", {}).get("wind", [])
        cond_data = (cond_r or {}).get("data", {}).get("conditions", [])
        tides_data = (tides_r or {}).get("data", {}).get("tides", [])

        wind_by_ts = {w["timestamp"]: w for w in wind_data}

        headline_by_date, dtw_by_date = {}, {}
        for c in cond_data:
            dk = datetime.fromtimestamp(c["timestamp"]).strftime("%Y-%m-%d")
            headline_by_date[dk] = (c.get("headline") or "").strip().rstrip(".")
            dtw_by_date[dk] = c.get("dayToWatch", False)

        low_tides = {}
        for t in tides_data:
            if t.get("type") == "LOW":
                t_dt = datetime.fromtimestamp(t["timestamp"])
                dk = t_dt.strftime("%Y-%m-%d")
                low_tides.setdefault(dk, []).append(t_dt)

        by_date = {}
        for w in wave_data:
            ts = w.get("timestamp", 0)
            dt = datetime.fromtimestamp(ts)
            dk = dt.strftime("%Y-%m-%d")
            if dk == today:
                continue
            surf = w.get("surf", {})
            wave_max = surf.get("max", 0)
            if wave_max < 1.5:
                continue
            wind_entry = wind_by_ts.get(ts, {})
            wind_kts = wind_entry.get("speed", 20)
            wind_dir = (wind_entry.get("directionType") or "cross-shore").lower()
            wind_mph = wind_kts * KTS_TO_MPH
            wind_mult = WIND_BONUS.get(wind_dir, 0.7)
            score = wave_max * wind_mult - max(0, wind_mph - 10) * 0.1
            if dtw_by_date.get(dk):
                score *= 1.3

            lows = low_tides.get(dk, [])
            if lows:
                mid = dt.replace(hour=10, minute=0)
                best_low = min(lows, key=lambda x: abs((x - mid).total_seconds()))
                go_hour = max(6, best_low.hour - 1)
                go_time = best_low.replace(hour=go_hour, minute=0).strftime("%-I:%M %p")
            else:
                go_time = "Early morning"

            if dk not in by_date or score > by_date[dk]["score"]:
                by_date[dk] = {
                    "spotName": name,
                    "dist": dist,
                    "date": dk,
                    "dateLabel": dt.strftime("%A, %b %-d"),
                    "waveMin": round(surf.get("min", 0)),
                    "waveMax": round(wave_max),
                    "waveRelation": surf.get("humanRelation", ""),
                    "windMph": round(wind_mph),
                    "windDir": wind_dir,
                    "headline": headline_by_date.get(dk, ""),
                    "dayToWatch": dtw_by_date.get(dk, False),
                    "goTime": go_time,
                    "score": score,
                }

        if by_date:
            best_per_spot.append(max(by_date.values(), key=lambda x: x["score"]))

        time.sleep(0.3)

    best_per_spot.sort(key=lambda x: x["score"], reverse=True)
    seen = set()
    top3 = []
    for e in best_per_spot:
        if e["spotName"] not in seen:
            seen.add(e["spotName"])
            top3.append(e)
        if len(top3) == 3:
            break
    return top3


def main():
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))

    print("Fetching current conditions...", file=sys.stderr)
    current = fetch_current(cfg)
    print(f"  {len(current)} spots found", file=sys.stderr)

    print("Fetching 7-day forecast for top spots...", file=sys.stderr)
    best3 = fetch_forecast(cfg, current)
    print(f"  {len(best3)} recommendations built", file=sys.stderr)

    # Top 10 current spots for the dashboard list
    top_current = [s for s in current if s["waveMax"] >= 1.0][:20]

    out = {
        "updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "updatedLabel": datetime.now().strftime("%-I:%M %p, %A %b %-d"),
        "current": top_current,
        "best3": best3,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Written to {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()

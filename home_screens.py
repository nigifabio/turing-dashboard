# Idle screens fed by Home Assistant: house sensors, and weather with the week's forecast.
# Used by sonos_dashboard.py. Configure under "homeassistant" in sonos_dashboard_config.json:
#   url, token            Home Assistant address and a long-lived access token
#   weather               weather entity (default weather.forecast_home)
#   location              label for the weather screen title
#   sensors               up to 4 big tiles: [label, entity_id, kind], kind = temp | humidity | pm25 | climate
#   devices               up to 6 status rows: [label, entity_id] (lights, fans, humidifiers, sensors)

import math
from datetime import datetime

import requests
from PIL import Image, ImageDraw, ImageFont

BG = (10, 10, 20)
WHITE, GREY, DIM = (255, 255, 255), (170, 170, 170), (110, 110, 120)
BLUE, GREEN, YELLOW, ORANGE, RED = (66, 133, 244), (52, 168, 83), (251, 188, 4), (255, 140, 0), (234, 67, 53)

# What the screens show; set from the config by configure(). Missing entities are skipped.
SENSORS = []   # (label, entity, kind)
DEVICES = []   # (label, entity)
WEATHER = "weather.forecast_home"
LOCATION = ""


def configure(cfg):
    global SENSORS, DEVICES, WEATHER, LOCATION
    SENSORS = [tuple(x) for x in cfg.get("sensors", [])][:4]
    DEVICES = [tuple(x) for x in cfg.get("devices", [])][:6]
    WEATHER = cfg.get("weather", WEATHER)
    LOCATION = cfg.get("location", "")

CONDITIONS = {
    "sunny": "Sunny", "clear-night": "Clear", "partlycloudy": "Partly cloudy", "cloudy": "Cloudy",
    "rainy": "Rain", "pouring": "Heavy rain", "snowy": "Snow", "snowy-rainy": "Sleet", "fog": "Fog",
    "hail": "Hail", "lightning": "Thunder", "lightning-rainy": "Thunderstorm", "windy": "Windy",
    "windy-variant": "Windy", "exceptional": "Warning",
}


class HomeAssistant:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def states(self):
        r = requests.get(f"{self.url}/api/states", headers=self.headers, timeout=8)
        r.raise_for_status()
        return {s["entity_id"]: s for s in r.json()}

    def forecast(self, kind="daily"):
        r = requests.post(f"{self.url}/api/services/weather/get_forecasts?return_response",
                          headers=self.headers, json={"entity_id": WEATHER, "type": kind}, timeout=10)
        r.raise_for_status()
        return r.json()["service_response"][WEATHER]["forecast"]


def _fonts(regular, bold):
    f = lambda path, size: ImageFont.truetype(path, size)
    return {"title": f(bold, 24), "big": f(bold, 40), "mid": f(bold, 22), "label": f(regular, 16),
            "small": f(regular, 13), "tiny": f(regular, 11)}


def _num(state):
    try:
        return float(state)
    except (TypeError, ValueError):
        return None


def _pm25_colour(v):
    return GREEN if v <= 15 else YELLOW if v <= 35 else ORANGE if v <= 55 else RED


def render_home_sensors(width, height, states, font_regular, font_bold):
    frame = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(frame)
    F = _fonts(font_regular, font_bold)
    d.text((20, 15), "HOME · sensors", font=F["title"], fill=WHITE)
    if not states:
        d.text((20, 60), "Home Assistant unreachable", font=F["label"], fill=RED)
        return frame

    # top: up to four big tiles
    tiles = []
    for label, eid, kind in SENSORS:
        s = states.get(eid)
        if not s or s["state"] in ("unavailable", "unknown"):
            continue
        if kind == "climate":
            room, target = s["attributes"].get("current_temperature"), s["attributes"].get("temperature")
            if room is None:
                continue
            tiles.append((label, f"{room:.1f}°", f"target {target:.1f}°" if target else "", ORANGE))
        elif kind == "temp":
            tiles.append((label, f"{_num(s['state']):.0f}°C", "inside", YELLOW))
        elif kind == "humidity":
            v = _num(s["state"])
            tiles.append((label, f"{v:.0f}%", "inside", BLUE if 40 <= v <= 60 else ORANGE))
        elif kind == "pm25":
            v = _num(s["state"])
            word = "good" if v <= 15 else "moderate" if v <= 35 else "poor" if v <= 55 else "bad"
            tiles.append((label, f"{v:.0f}", f"µg/m³ · {word}", _pm25_colour(v)))
    w = (width - 40 - 10 * (max(len(tiles), 1) - 1)) // max(len(tiles), 1)
    for i, (label, value, sub, colour) in enumerate(tiles[:4]):
        x = 20 + i * (w + 10)
        d.rounded_rectangle([x, 55, x + w, 155], radius=10, fill=(26, 26, 40))
        d.text((x + 10, 62), label, font=F["small"], fill=GREY)
        d.text((x + 10, 80), value, font=F["big"] if len(value) <= 5 else F["mid"], fill=colour)
        d.text((x + 10, 132), sub, font=F["tiny"], fill=DIM)

    # outside, from the weather entity
    wx = states.get(WEATHER)
    if wx:
        a = wx["attributes"]
        line = f"Outside {a.get('temperature', '–')}°C · {a.get('humidity', '–')}% · {CONDITIONS.get(wx['state'], wx['state'])}"
        d.text((20, 165), line, font=F["label"], fill=GREY)

    # bottom: device states in two columns
    y0 = 195
    for i, (label, eid) in enumerate(DEVICES):
        s = states.get(eid)
        if not s:
            continue
        col, row = i % 2, i // 2
        x, y = 20 + col * (width // 2 - 10), y0 + row * 34
        st, a = s["state"], s["attributes"]
        if eid.startswith("light."):
            on = st == "on"
            text, colour = ("On" if on else "Off"), (tuple(a["rgb_color"]) if on and a.get("rgb_color") else DIM)
            if on and a.get("effect") not in (None, "None"):
                # Roboto has no music-note glyph; keep it short so it fits beside the name
                text = "Music" if "music" in a["effect"].lower() else a["effect"][:10]
        elif eid.startswith("fan."):
            text, colour = (a.get("preset_mode") or "On") if st == "on" else "Off", GREEN if st == "on" else DIM
        elif eid.startswith("humidifier."):
            text, colour = ("On" if st == "on" else "Off"), BLUE if st == "on" else DIM
        elif "filter" in eid:
            v = _num(st)
            text, colour = f"{v:.0f}%" if v is not None else st, GREEN if (v or 0) > 20 else RED
        else:
            text, colour = st, RED if st.lower() in ("removed", "empty") else GREY
        d.ellipse([x, y + 5, x + 10, y + 15], fill=colour)
        d.text((x + 18, y), label, font=F["label"], fill=WHITE)
        d.text((x + width // 2 - 50 - d.textlength(text, font=F["label"]), y), text, font=F["label"], fill=GREY)

    d.text((width - 20 - d.textlength(datetime.now().strftime("%H:%M"), font=F["small"]), 22),
           datetime.now().strftime("%H:%M"), font=F["small"], fill=DIM)
    return frame


# ---- weather -------------------------------------------------------------------------
def _icon(d, cx, cy, r, cond):
    """Small drawn weather icon centred on (cx, cy), radius r."""
    def sun(x, y, rr):
        d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=YELLOW)
        for k in range(8):
            a = k * math.pi / 4
            d.line([x + math.cos(a) * rr * 1.35, y + math.sin(a) * rr * 1.35,
                    x + math.cos(a) * rr * 1.75, y + math.sin(a) * rr * 1.75], fill=YELLOW, width=2)

    def cloud(x, y, rr, colour=(200, 205, 215)):
        d.ellipse([x - rr * 1.1, y - rr * 0.2, x - rr * 0.1, y + rr * 0.7], fill=colour)
        d.ellipse([x - rr * 0.6, y - rr * 0.75, x + rr * 0.5, y + rr * 0.45], fill=colour)
        d.ellipse([x + rr * 0.05, y - rr * 0.35, x + rr * 1.1, y + rr * 0.7], fill=colour)
        d.rectangle([x - rr * 0.6, y + rr * 0.1, x + rr * 0.6, y + rr * 0.7], fill=colour)

    def drops(x, y, rr, n, colour=BLUE):
        for k in range(n):
            dx = x - rr * 0.6 + k * rr * 0.6
            d.line([dx, y + rr * 0.85, dx - rr * 0.2, y + rr * 1.35], fill=colour, width=2)

    if cond == "sunny":
        sun(cx, cy, r * 0.55)
    elif cond == "clear-night":
        d.ellipse([cx - r * 0.6, cy - r * 0.6, cx + r * 0.6, cy + r * 0.6], fill=(230, 230, 200))
        d.ellipse([cx - r * 0.25, cy - r * 0.8, cx + r * 0.8, cy + r * 0.3], fill=BG)
    elif cond == "partlycloudy":
        sun(cx - r * 0.35, cy - r * 0.3, r * 0.38)
        cloud(cx + r * 0.1, cy + r * 0.1, r * 0.6)
    elif cond in ("rainy", "pouring", "lightning-rainy", "hail", "snowy-rainy"):
        cloud(cx, cy - r * 0.25, r * 0.7, (160, 165, 180))
        drops(cx, cy - r * 0.25, r * 0.7, 3 if cond != "pouring" else 4)
        if cond == "lightning-rainy":
            d.polygon([(cx, cy + r * 0.2), (cx - r * 0.2, cy + r * 0.6), (cx, cy + r * 0.6), (cx - r * 0.15, cy + r)], fill=YELLOW)
    elif cond == "snowy":
        cloud(cx, cy - r * 0.25, r * 0.7, (160, 165, 180))
        for k in range(3):
            x = cx - r * 0.45 + k * r * 0.45
            d.text((x - 4, cy + r * 0.3), "*", fill=WHITE)
    elif cond == "lightning":
        cloud(cx, cy - r * 0.25, r * 0.7, (130, 135, 150))
        d.polygon([(cx, cy + r * 0.1), (cx - r * 0.25, cy + r * 0.55), (cx, cy + r * 0.55), (cx - r * 0.15, cy + r)], fill=YELLOW)
    elif cond == "fog":
        for k in range(4):
            d.line([cx - r * 0.7, cy - r * 0.4 + k * r * 0.28, cx + r * 0.7, cy - r * 0.4 + k * r * 0.28], fill=GREY, width=3)
    else:  # cloudy, windy, exceptional, unknown
        cloud(cx, cy, r * 0.75)


def render_weather(width, height, states, daily, font_regular, font_bold):
    frame = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(frame)
    F = _fonts(font_regular, font_bold)
    d.text((20, 15), f"WEATHER · {LOCATION}" if LOCATION else "WEATHER", font=F["title"], fill=WHITE)
    wx = (states or {}).get(WEATHER)
    if not wx:
        d.text((20, 60), "Home Assistant unreachable", font=F["label"], fill=RED)
        return frame
    a = wx["attributes"]

    # now
    _icon(d, 70, 105, 42, wx["state"])
    d.text((130, 62), f"{a.get('temperature', '–')}°", font=F["big"], fill=WHITE)
    d.text((130, 110), CONDITIONS.get(wx["state"], wx["state"]), font=F["label"], fill=GREY)
    today = (daily or [{}])[0]
    if today.get("temperature") is not None:
        d.text((130, 132), f"max {today['temperature']:.0f}°  min {today.get('templow', 0):.0f}°", font=F["label"], fill=GREY)
    details = [f"Humidity {a.get('humidity', '–')}%", f"Wind {a.get('wind_speed', '–')} km/h",
               f"Pressure {a.get('pressure', '–'):.0f} hPa" if isinstance(a.get("pressure"), (int, float)) else ""]
    for i, t in enumerate(x for x in details if x):
        d.text((300, 66 + i * 24), t, font=F["small"], fill=GREY)

    # the week
    days = (daily or [])[:7]
    if days:
        n = len(days)
        cw = (width - 40) // n
        temps = [x.get("temperature") for x in days if x.get("temperature") is not None]
        lows = [x.get("templow") for x in days if x.get("templow") is not None]
        tmin, tmax = min(lows + temps), max(temps + lows)
        span = max(tmax - tmin, 1)
        top = 168
        d.line([20, top - 6, width - 20, top - 6], fill=(40, 40, 55))
        for i, f in enumerate(days):
            x = 20 + i * cw
            cx = x + cw // 2
            dt = datetime.fromisoformat(f["datetime"].replace("Z", "+00:00")).astimezone()
            name = "Today" if i == 0 else dt.strftime("%a")
            d.text((cx - d.textlength(name, font=F["small"]) / 2, top), name, font=F["small"], fill=WHITE if i == 0 else GREY)
            _icon(d, cx, top + 42, 17, f.get("condition", "cloudy"))
            hi, lo = f.get("temperature"), f.get("templow")
            # a small bar showing the day's range on the week's scale
            if hi is not None and lo is not None:
                y_hi = top + 72 + int((tmax - hi) / span * 50)
                y_lo = top + 72 + int((tmax - lo) / span * 50)
                d.rounded_rectangle([cx - 3, y_hi, cx + 3, max(y_lo, y_hi + 4)], radius=3, fill=ORANGE)
                d.text((cx - d.textlength(f"{hi:.0f}°", font=F["tiny"]) / 2, y_hi - 14), f"{hi:.0f}°", font=F["tiny"], fill=WHITE)
                d.text((cx - d.textlength(f"{lo:.0f}°", font=F["tiny"]) / 2, max(y_lo, y_hi + 4) + 2), f"{lo:.0f}°", font=F["tiny"], fill=GREY)
            rain = f.get("precipitation") or 0
            if rain >= 0.5:
                t = f"{rain:.0f} mm" if rain >= 1 else f"{rain:.1f} mm"
                d.text((cx - d.textlength(t, font=F["tiny"]) / 2, height - 18), t, font=F["tiny"], fill=BLUE)
    return frame

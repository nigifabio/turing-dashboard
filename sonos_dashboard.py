#!/usr/bin/env python3
# Multi-source "Now Playing" dashboard for the Turing Smart Screen.
# Priority: Sonos (music, alternating with the house sensors and weather when Home
# Assistant is configured) > Jellyfin (movie/show) > idle screens, cycling
# between Proxmox host stats, a Prometheus bandwidth graph, the house sensors and
# the weather forecast (the last two from Home Assistant, see home_screens.py).

import io
import json
import os
import signal
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import soco
import urllib3
from PIL import Image, ImageDraw, ImageFont
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# SoCo's own default REQUEST_TIMEOUT is 20s per SOAP call; with 5+ devices
# to poll every loop, a couple of sleeping/offline ones (e.g. a portable
# Roam) can stall the whole dashboard for over a minute. Shorten it.
soco.config.REQUEST_TIMEOUT = 3.0
socket.setdefaulttimeout(8)

REPO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_DIR))

from library.lcd.lcd_comm_rev_a import LcdCommRevA, Orientation  # noqa: E402
from library.log import logger  # noqa: E402
from home_screens import HomeAssistant, configure as configure_home_screens, render_home_sensors, render_weather  # noqa: E402

CONFIG_PATH = REPO_DIR / "sonos_dashboard_config.json"
ART_CACHE_DIR = REPO_DIR / "art_cache"
ART_CACHE_DIR.mkdir(exist_ok=True)
ART_CACHE_MAX_FILES = 20

FONT_DIR = REPO_DIR / "res" / "fonts" / "roboto"
FONT_BOLD = str(FONT_DIR / "Roboto-Bold.ttf")
FONT_REGULAR = str(FONT_DIR / "Roboto-Regular.ttf")

POLL_INTERVAL_S = 2
PROXMOX_POLL_INTERVAL_S = 10
SONOS_REDISCOVER_INTERVAL_S = 60

stop = False


def sighandler(signum, frame):
    global stop
    stop = True


signal.signal(signal.SIGINT, sighandler)
signal.signal(signal.SIGTERM, sighandler)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


config = load_config()
PVE = config["proxmox"]
PVE_BASE = f"https://{PVE['host']}:8006/api2/json"
PVE_HEADERS = {"Authorization": f"PVEAPIToken={PVE['token_id']}={PVE['token_secret']}"}

JF = config["jellyfin"]
JELLYFIN_BASE = f"http://{JF['host']}:{JF['port']}"
# Jellyfin 12 dropped X-Emby-Token; only the Authorization header is accepted.
JELLYFIN_HEADERS = {"Authorization": f'MediaBrowser Token="{JF["api_key"]}"'}

PROM = config["prometheus"]
PROM_BASE = f"http://{PROM['host']}:{PROM['port']}"
PROM_INSTANCE = PROM["host_instance"]
PROM_DEVICE = PROM["host_device"]

JELLYFIN_POLL_INTERVAL_S = 5
BANDWIDTH_POLL_INTERVAL_S = 30
BANDWIDTH_WINDOW_S = 30 * 60
IDLE_SCREEN_CYCLE_S = 8
HA_STATES_POLL_INTERVAL_S = 30
HA_FORECAST_POLL_INTERVAL_S = 15 * 60
HA = None
if "homeassistant" in config:  # optional: house sensors + weather screens
    HA = HomeAssistant(config["homeassistant"]["url"], config["homeassistant"]["token"])
    configure_home_screens(config["homeassistant"])

SP = config["spotify"]
spotify_client = Spotify(auth_manager=SpotifyOAuth(
    client_id=SP["client_id"],
    client_secret=SP["client_secret"],
    redirect_uri=SP["redirect_uri"],
    scope="user-read-playback-state user-read-currently-playing",
    cache_path=str(REPO_DIR / ".spotify-cache"),
    open_browser=False,
))


def get_spotify_up_next():
    # Only Spotify's own API knows the next track during Spotify Connect
    # playback (Sonos's queue is empty in that mode - it doesn't control it).
    try:
        q = spotify_client.queue()
        items = q.get("queue") or []
        if not items:
            return None, None
        item = items[0]
        return item.get("name"), ", ".join(a["name"] for a in item.get("artists", []))
    except Exception as e:
        logger.warning(f"Spotify queue query failed: {e}")
        return None, None


# --- Sonos ---
# Tracks ALL Sonos players on the LAN and shows whichever one is actually
# playing (there's usually only one active at a time in a multi-room setup).

class SonosSource:
    def __init__(self):
        self.devices = {}  # player_name -> SoCo instance
        self.last_discover = 0.0
        self._discover()

    def _discover(self):
        self.last_discover = time.time()
        try:
            found = soco.discover(timeout=5) or set()
            self.devices = {d.player_name: d for d in found}
            logger.info(f"Sonos discovered: {', '.join(self.devices) or '(none)'}")
        except Exception as e:
            logger.warning(f"Sonos discovery failed: {e}")

    def get_now_playing(self):
        if time.time() - self.last_discover > SONOS_REDISCOVER_INTERVAL_S:
            self._discover()

        for name, device in list(self.devices.items()):
            try:
                transport = device.get_current_transport_info()
                if transport.get("current_transport_state") != "PLAYING":
                    continue
                track = device.get_current_track_info()
            except Exception as e:
                logger.warning(f"Sonos '{name}' query failed: {e}")
                continue

            if not track.get("title"):
                continue

            next_title, next_artist = self._get_up_next(device, track)

            try:
                volume = device.volume
            except Exception:
                volume = None

            try:
                group = device.group
                room_label = group.short_label if group and len(group.members) > 1 else name
            except Exception:
                room_label = name

            return {
                "room": room_label,
                "volume": volume,
                "title": track.get("title") or "",
                "artist": track.get("artist") or "",
                "album": track.get("album") or "",
                "art_url": track.get("album_art") or None,
                "position_s": parse_hms(track.get("position", "0:00:00")),
                "duration_s": parse_hms(track.get("duration", "0:00:00")),
                "track_key": track.get("uri") or track.get("title"),
                "next_title": next_title,
                "next_artist": next_artist,
            }
        return None

    @staticmethod
    def _get_up_next(device, track):
        # Sonos's own queue only reflects "up next" for classic queue-based
        # playback (album/playlist browsed from the Sonos app). Spotify
        # Connect streams (x-sonos-vli:...spotify:...) leave Sonos's queue
        # empty/irrelevant - Spotify's servers control what's next, not
        # Sonos - so fall back to Spotify's own queue API for those.
        try:
            pos = int(track.get("playlist_position", 0))
        except (TypeError, ValueError):
            pos = 0
        if pos > 0:
            try:
                queue = device.get_queue(start=pos, max_items=1)
            except Exception:
                queue = None
            if queue:
                item = queue[0]
                return (getattr(item, "title", None), getattr(item, "creator", None))

        uri = track.get("uri") or ""
        if "spotify:" in uri:
            return get_spotify_up_next()

        return None, None


def parse_hms(s):
    try:
        parts = [int(p) for p in s.split(":")]
    except ValueError:
        return 0
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts[-3:]
    return h * 3600 + m * 60 + sec


def get_cached_image(url, cache_key, headers=None):
    if not url or not cache_key:
        return None
    safe_key = "".join(c if c.isalnum() else "_" for c in cache_key)[:120]
    cache_file = ART_CACHE_DIR / f"{safe_key}.jpg"
    if cache_file.exists():
        try:
            return Image.open(cache_file).convert("RGB")
        except Exception:
            cache_file.unlink(missing_ok=True)

    try:
        resp = requests.get(url, headers=headers, timeout=5)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        img.save(cache_file)
        files = sorted(ART_CACHE_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
        for old in files[:-ART_CACHE_MAX_FILES]:
            old.unlink(missing_ok=True)
        return img
    except Exception as e:
        logger.warning(f"Image download failed ({cache_key}): {e}")
        return None


def fit_square(img, size):
    w, h = img.size
    if w != h:
        m = min(w, h)
        left, top = (w - m) // 2, (h - m) // 2
        img = img.crop((left, top, left + m, top + m))
    return img.resize((size, size))


# --- Proxmox ---

def get_proxmox_stats():
    try:
        r = requests.get(
            f"{PVE_BASE}/nodes/{PVE['node']}/status",
            headers=PVE_HEADERS, verify=False, timeout=5,
        )
        r.raise_for_status()
        d = r.json()["data"]
        mem = d["memory"]
        rootfs = d.get("rootfs", {})
        return {
            "cpu_pct": d["cpu"] * 100,
            "mem_used_gb": mem["used"] / 1024 ** 3,
            "mem_total_gb": mem["total"] / 1024 ** 3,
            "disk_used_gb": rootfs.get("used", 0) / 1024 ** 3,
            "disk_total_gb": max(rootfs.get("total", 1), 1) / 1024 ** 3,
            "uptime_s": d.get("uptime", 0),
        }
    except Exception as e:
        logger.warning(f"Proxmox API error: {e}")
        return None


# --- Jellyfin ---

def get_jellyfin_now_playing():
    try:
        r = requests.get(f"{JELLYFIN_BASE}/Sessions", headers=JELLYFIN_HEADERS, timeout=5)
        r.raise_for_status()
        sessions = r.json()
    except Exception as e:
        logger.warning(f"Jellyfin API error: {e}")
        return None

    for s in sessions:
        item = s.get("NowPlayingItem")
        play_state = s.get("PlayState") or {}
        if not item or play_state.get("IsPaused"):
            continue

        if item.get("Type") == "Episode":
            title = item.get("SeriesName") or item.get("Name") or ""
            subtitle = item.get("Name") or ""
            season, episode = item.get("ParentIndexNumber"), item.get("IndexNumber")
            if season is not None and episode is not None:
                subtitle = f"S{season:02d}E{episode:02d} · {subtitle}"
        else:
            title = item.get("Name") or ""
            subtitle = str(item.get("ProductionYear") or "")

        item_id = item.get("Id")
        art_url = (
            f"{JELLYFIN_BASE}/Items/{item_id}/Images/Primary"
            if item_id else None
        )

        return {
            "device": s.get("DeviceName") or s.get("Client") or "Jellyfin",
            "title": title,
            "subtitle": subtitle,
            "art_url": art_url,
            "track_key": item_id or title,
            "position_s": (play_state.get("PositionTicks") or 0) / 10_000_000,
            "duration_s": (item.get("RunTimeTicks") or 0) / 10_000_000,
        }
    return None


# --- Bandwidth (Prometheus) ---

def get_bandwidth_history():
    try:
        end = time.time()
        start = end - BANDWIDTH_WINDOW_S
        rx = _prom_range(f'rate(node_network_receive_bytes_total{{instance="{PROM_INSTANCE}",device="{PROM_DEVICE}"}}[2m])*8/1e6', start, end)
        tx = _prom_range(f'rate(node_network_transmit_bytes_total{{instance="{PROM_INSTANCE}",device="{PROM_DEVICE}"}}[2m])*8/1e6', start, end)
        if rx is None or tx is None:
            return None
        return {"rx_mbps": rx, "tx_mbps": tx}
    except Exception as e:
        logger.warning(f"Prometheus query failed: {e}")
        return None


def _prom_range(query, start, end, step=60):
    r = requests.get(
        f"{PROM_BASE}/api/v1/query_range",
        params={"query": query, "start": start, "end": end, "step": step},
        timeout=5,
    )
    r.raise_for_status()
    data = r.json()
    result = data.get("data", {}).get("result", [])
    if not result:
        return []
    return [float(v) for _, v in result[0]["values"]]


def fmt_uptime(seconds):
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    return f"{days}d {hours}h"


def fmt_mmss(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


# --- Rendering ---

def wrap_text(draw, text, font, max_width, max_lines):
    lines = []
    words = text.split()
    cur = ""
    for w in words:
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=font) <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
        if len(lines) >= max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines


def render_now_playing(width, height, np):
    frame = Image.new("RGB", (width, height), (10, 10, 10))
    draw = ImageDraw.Draw(frame)

    art_size = height - 40
    art = get_cached_image(np["art_url"], f"sonos_{np['track_key']}")
    if art:
        art = fit_square(art, art_size)
        frame.paste(art, (20, 20))
    else:
        draw.rectangle([20, 20, 20 + art_size, 20 + art_size], fill=(40, 40, 40))

    text_x = 20 + art_size + 20
    max_w = width - text_x - 20

    room_font = ImageFont.truetype(FONT_REGULAR, 13)
    title_font = ImageFont.truetype(FONT_BOLD, 26)
    artist_font = ImageFont.truetype(FONT_REGULAR, 18)
    album_font = ImageFont.truetype(FONT_REGULAR, 14)
    time_font = ImageFont.truetype(FONT_REGULAR, 12)

    y = 20
    room_line = np["room"].upper()
    if np.get("volume") is not None:
        room_line += f"   ·   VOL {np['volume']}"
    draw.text((text_x, y), room_line, font=room_font, fill=(30, 215, 96))
    y += 24
    for line in wrap_text(draw, np["title"], title_font, max_w, 3):
        draw.text((text_x, y), line, font=title_font, fill=(255, 255, 255))
        y += 32
    y += 6
    for line in wrap_text(draw, np["artist"], artist_font, max_w, 2):
        draw.text((text_x, y), line, font=artist_font, fill=(200, 200, 200))
        y += 24
    y += 4
    for line in wrap_text(draw, np["album"], album_font, max_w, 2):
        draw.text((text_x, y), line, font=album_font, fill=(140, 140, 140))
        y += 18

    if np.get("next_title"):
        next_font = ImageFont.truetype(FONT_REGULAR, 11)
        next_text = "Next: " + np["next_title"]
        if np.get("next_artist"):
            next_text += f" — {np['next_artist']}"
        y += 8
        for line in wrap_text(draw, next_text, next_font, max_w, 1):
            draw.text((text_x, y), line, font=next_font, fill=(110, 110, 110))

    bar_x, bar_w, bar_h = text_x, max_w, 8
    bar_y = height - 40
    draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], radius=4, fill=(50, 50, 50))
    if np["duration_s"]:
        frac = min(1.0, np["position_s"] / np["duration_s"])
        draw.rounded_rectangle(
            [bar_x, bar_y, bar_x + int(bar_w * frac), bar_y + bar_h],
            radius=4, fill=(30, 215, 96),
        )

    draw.text((bar_x, bar_y - 18), fmt_mmss(np["position_s"]), font=time_font, fill=(180, 180, 180))
    dur_text = fmt_mmss(np["duration_s"])
    draw.text(
        (bar_x + bar_w - draw.textlength(dur_text, font=time_font), bar_y - 18),
        dur_text, font=time_font, fill=(180, 180, 180),
    )

    return frame


JELLYFIN_ACCENT = (0, 164, 220)


def render_jellyfin_now_playing(width, height, jf):
    frame = Image.new("RGB", (width, height), (10, 10, 10))
    draw = ImageDraw.Draw(frame)

    art_size = height - 40
    art = get_cached_image(jf["art_url"], f"jf_{jf['track_key']}", headers=JELLYFIN_HEADERS)
    if art:
        art = fit_square(art, art_size)
        frame.paste(art, (20, 20))
    else:
        draw.rectangle([20, 20, 20 + art_size, 20 + art_size], fill=(40, 40, 40))

    text_x = 20 + art_size + 20
    max_w = width - text_x - 20

    device_font = ImageFont.truetype(FONT_REGULAR, 13)
    title_font = ImageFont.truetype(FONT_BOLD, 24)
    subtitle_font = ImageFont.truetype(FONT_REGULAR, 16)
    time_font = ImageFont.truetype(FONT_REGULAR, 12)

    y = 20
    draw.text((text_x, y), f"JELLYFIN · {jf['device']}".upper(), font=device_font, fill=JELLYFIN_ACCENT)
    y += 24
    for line in wrap_text(draw, jf["title"], title_font, max_w, 3):
        draw.text((text_x, y), line, font=title_font, fill=(255, 255, 255))
        y += 30
    y += 6
    if jf["subtitle"]:
        for line in wrap_text(draw, jf["subtitle"], subtitle_font, max_w, 2):
            draw.text((text_x, y), line, font=subtitle_font, fill=(200, 200, 200))
            y += 22

    bar_x, bar_w, bar_h = text_x, max_w, 8
    bar_y = height - 40
    draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], radius=4, fill=(50, 50, 50))
    if jf["duration_s"]:
        frac = min(1.0, jf["position_s"] / jf["duration_s"])
        draw.rounded_rectangle(
            [bar_x, bar_y, bar_x + int(bar_w * frac), bar_y + bar_h],
            radius=4, fill=JELLYFIN_ACCENT,
        )

    draw.text((bar_x, bar_y - 18), fmt_mmss(jf["position_s"]), font=time_font, fill=(180, 180, 180))
    dur_text = fmt_mmss(jf["duration_s"])
    draw.text(
        (bar_x + bar_w - draw.textlength(dur_text, font=time_font), bar_y - 18),
        dur_text, font=time_font, fill=(180, 180, 180),
    )

    return frame


def render_bandwidth_graph(width, height, bw):
    frame = Image.new("RGB", (width, height), (10, 10, 20))
    draw = ImageDraw.Draw(frame)
    title_font = ImageFont.truetype(FONT_BOLD, 24)
    label_font = ImageFont.truetype(FONT_REGULAR, 16)
    small_font = ImageFont.truetype(FONT_REGULAR, 11)

    draw.text((20, 15), "BANDWIDTH · server", font=title_font, fill=(255, 255, 255))

    if not bw or not bw.get("rx_mbps") or not bw.get("tx_mbps"):
        draw.text((20, 60), "Unreachable", font=label_font, fill=(255, 80, 80))
        return frame

    rx, tx = bw["rx_mbps"], bw["tx_mbps"]
    cur_rx, cur_tx = rx[-1], tx[-1]

    rx_color, tx_color = (66, 133, 244), (251, 188, 4)
    draw.text((20, 55), f"↓ {cur_rx:.1f} Mbps", font=label_font, fill=rx_color)
    draw.text((width // 2, 55), f"↑ {cur_tx:.1f} Mbps", font=label_font, fill=tx_color)

    plot_x, plot_y, plot_w, plot_h = 20, 100, width - 40, 160
    draw.rectangle([plot_x, plot_y, plot_x + plot_w, plot_y + plot_h], outline=(60, 60, 60))

    max_val = max(max(rx, default=0), max(tx, default=0), 1.0)

    def to_points(series):
        n = len(series)
        if n < 2:
            return []
        pts = []
        for i, v in enumerate(series):
            x = plot_x + int(i / (n - 1) * plot_w)
            y = plot_y + plot_h - int(min(1.0, v / max_val) * plot_h)
            pts.append((x, y))
        return pts

    rx_pts = to_points(rx)
    tx_pts = to_points(tx)
    if len(rx_pts) >= 2:
        draw.line(rx_pts, fill=rx_color, width=2)
    if len(tx_pts) >= 2:
        draw.line(tx_pts, fill=tx_color, width=2)

    draw.text((plot_x, plot_y + plot_h + 6), "-30 min", font=small_font, fill=(140, 140, 140))
    now_text = "now"
    draw.text((plot_x + plot_w - draw.textlength(now_text, font=small_font), plot_y + plot_h + 6), now_text, font=small_font, fill=(140, 140, 140))

    return frame


def render_proxmox_stats(width, height, stats):
    frame = Image.new("RGB", (width, height), (10, 10, 20))
    draw = ImageDraw.Draw(frame)
    title_font = ImageFont.truetype(FONT_BOLD, 24)
    label_font = ImageFont.truetype(FONT_REGULAR, 16)

    draw.text((20, 15), "PROXMOX · server", font=title_font, fill=(255, 255, 255))

    if not stats:
        draw.text((20, 60), "Unreachable", font=label_font, fill=(255, 80, 80))
        return frame

    def bar(y, label, frac, value_text, color):
        draw.text((20, y), label, font=label_font, fill=(200, 200, 200))
        bx, by, bw, bh = 20, y + 24, width - 40, 18
        draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=6, fill=(40, 40, 40))
        draw.rounded_rectangle([bx, by, bx + int(bw * min(1.0, frac)), by + bh], radius=6, fill=color)
        draw.text((bx + bw - draw.textlength(value_text, font=label_font), y), value_text, font=label_font, fill=(255, 255, 255))

    bar(60, "CPU", stats["cpu_pct"] / 100, f"{stats['cpu_pct']:.0f}%", (66, 133, 244))
    bar(120, "RAM", stats["mem_used_gb"] / stats["mem_total_gb"], f"{stats['mem_used_gb']:.1f}/{stats['mem_total_gb']:.1f} GB", (52, 168, 83))
    bar(180, "DISK", stats["disk_used_gb"] / stats["disk_total_gb"], f"{stats['disk_used_gb']:.0f}/{stats['disk_total_gb']:.0f} GB", (251, 188, 4))

    draw.text((20, 250), f"Uptime: {fmt_uptime(stats['uptime_s'])}", font=label_font, fill=(180, 180, 180))
    draw.text((20, 275), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), font=label_font, fill=(140, 140, 140))

    return frame


COM_PORT = "/dev/ttyACM0"


def device_id():
    """Identity of the current USB screen node; changes when the screen re-enumerates."""
    try:
        st = os.stat(COM_PORT)
        return (st.st_rdev, st.st_ctime_ns)
    except FileNotFoundError:
        return None


def main():
    lcd_comm = LcdCommRevA(com_port=COM_PORT, display_width=320, display_height=480)
    lcd_comm.Reset()
    lcd_comm.InitializeComm()
    lcd_comm.SetBrightness(level=20)
    lcd_comm.SetOrientation(orientation=Orientation.LANDSCAPE)

    width, height = lcd_comm.get_width(), lcd_comm.get_height()
    screen_id = device_id()

    sonos = SonosSource()

    proxmox_cache = None
    last_proxmox_poll = 0.0
    jellyfin_cache = None
    last_jellyfin_poll = 0.0
    bandwidth_cache = None
    last_bandwidth_poll = 0.0
    ha_states = None
    last_ha_poll = 0.0
    ha_daily = None
    last_forecast_poll = 0.0

    while not stop:
        loop_start = time.perf_counter()
        now = time.time()

        if HA and now - last_ha_poll > HA_STATES_POLL_INTERVAL_S:
            last_ha_poll = now
            try:
                ha_states = HA.states()
            except Exception as e:
                logger.warning(f"Home Assistant states failed: {e}")
                ha_states = None
        if HA and now - last_forecast_poll > HA_FORECAST_POLL_INTERVAL_S:
            last_forecast_poll = now
            try:
                ha_daily = HA.forecast("daily")
            except Exception as e:
                logger.warning(f"Home Assistant forecast failed: {e}")

        np = sonos.get_now_playing()
        if np:
            # while music plays: now playing for two slots, then the house sensors and the weather
            rotation = ["music", "music"] + (["sensors", "weather"] if HA else [])
            slot = rotation[int(now // IDLE_SCREEN_CYCLE_S) % len(rotation)]
            if slot == "sensors":
                frame = render_home_sensors(width, height, ha_states, FONT_REGULAR, FONT_BOLD)
            elif slot == "weather":
                frame = render_weather(width, height, ha_states, ha_daily, FONT_REGULAR, FONT_BOLD)
            else:
                frame = render_now_playing(width, height, np)
        else:
            if now - last_jellyfin_poll > JELLYFIN_POLL_INTERVAL_S:
                jellyfin_cache = get_jellyfin_now_playing()
                last_jellyfin_poll = now

            if jellyfin_cache:
                frame = render_jellyfin_now_playing(width, height, jellyfin_cache)
            else:
                if proxmox_cache is None or now - last_proxmox_poll > PROXMOX_POLL_INTERVAL_S:
                    proxmox_cache = get_proxmox_stats()
                    last_proxmox_poll = now
                if bandwidth_cache is None or now - last_bandwidth_poll > BANDWIDTH_POLL_INTERVAL_S:
                    bandwidth_cache = get_bandwidth_history()
                    last_bandwidth_poll = now

                screens = ["proxmox", "bandwidth"] + (["sensors", "weather"] if HA else [])
                screen = screens[int(now // IDLE_SCREEN_CYCLE_S) % len(screens)]
                if screen == "proxmox":
                    frame = render_proxmox_stats(width, height, proxmox_cache)
                elif screen == "bandwidth":
                    frame = render_bandwidth_graph(width, height, bandwidth_cache)
                elif screen == "sensors":
                    frame = render_home_sensors(width, height, ha_states, FONT_REGULAR, FONT_BOLD)
                else:
                    frame = render_weather(width, height, ha_states, ha_daily, FONT_REGULAR, FONT_BOLD)

        # After a USB reset (e.g. undervoltage) the screen comes back in its default
        # portrait/brightness state, so exit and let systemd re-initialise it.
        if device_id() != screen_id:
            logger.warning("Screen re-enumerated on USB, exiting so systemd restarts the dashboard")
            sys.exit(1)
        lcd_comm.DisplayPILImage(frame)

        elapsed = time.perf_counter() - loop_start
        time.sleep(max(0.0, POLL_INTERVAL_S - elapsed))

    lcd_comm.closeSerial()


if __name__ == "__main__":
    main()

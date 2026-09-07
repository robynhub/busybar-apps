#!/usr/bin/env python3
"""FRT Ticker — Internet routing-table telemetry for BUSY Bar.

Lightweight live mode using APNIC/Thyme routing summaries.
Statistics are read from APNIC's daily BGP Routing Table Analysis (Thyme):

  * IPv4/IPv6 announced prefixes
  * combined FRT size
  * RPKI Valid / Unknown / Invalid counts
  * average AS-path length (including prepends)
  * top IPv4 and IPv6 origin ASes by number of announced prefixes
  * worldwide IPv6 adoption (Google users accessing Google over IPv6)

Examples:

    python3 app.py --host 127.0.0.1:8080          # live (default)
    python3 app.py --host 127.0.0.1:8080 --demo
    python3 app.py --host 127.0.0.1:8080 --live --speed 6
    python3 app.py --host 127.0.0.1:8080 --live --autorefresh 60

--speed is the minimum persistence time, in seconds, of each screen.
Top Origin screens stay visible for twice the normal screen dwell time.
--autorefresh is the live-data refresh interval in minutes (default: 60; 0 disables).

Rendering deliberately uses BUSY Bar native text elements for strings/numbers.
Only decorative pixel graphics are rasterized into a 72x16 background image.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import random
import re
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

APP = "frt-ticker"
W, H = 72, 16
DEFAULT_HOST = "10.0.4.20"
CACHE_NAME = ".frt-ticker-cache.json"

APNIC_BASE = "https://thyme.apnic.net/current"
APNIC_V4_SUMMARY = APNIC_BASE + "/data-summary"
APNIC_V6_SUMMARY = APNIC_BASE + "/ipv6-summary"
APNIC_TOP_ORIGIN_V4 = APNIC_BASE + "/data-ASnet"
APNIC_TOP_ORIGIN_V6 = APNIC_BASE + "/ipv6-asn-table"
GOOGLE_IPV6_STATS = "https://www.google.com/intl/en/ipv6/statistics.html"

# Decorative palette (native text colors are specified independently below).
BLACK = (0, 0, 0)
DIM = (42, 55, 70)
BLUE = (0, 145, 255)
CYAN = (0, 230, 255)
VIOLET = (135, 72, 255)
MAGENTA = (240, 48, 180)
GREEN = (36, 220, 105)
AMBER = (255, 174, 0)
RED = (255, 50, 66)
DARK_BLUE = (4, 24, 45)
DARK_GREEN = (2, 33, 20)
DARK_AMBER = (43, 27, 0)
DARK_RED = (42, 4, 8)

# BUSY native-text colors (#RRGGBBAA).
C_WHITE = "#EEF6FFFF"
C_DIM = "#63758AFF"
C_BLUE = "#0091FFFF"
C_CYAN = "#00E6FFFF"
C_VIOLET = "#8750FFFF"
C_GREEN = "#24DC69FF"
C_AMBER = "#FFAE00FF"
C_RED = "#FF3242FF"


@dataclasses.dataclass
class Metrics:
    ipv4: int = 0
    ipv6: int = 0
    rpki_valid: int | None = None
    rpki_unknown: int | None = None
    rpki_invalid: int | None = None
    path_avg_prepend: float | None = None
    top_asn: int | None = None
    top_name: str = ""
    top_prefixes: int | None = None
    top6_asn: int | None = None
    top6_name: str = ""
    top6_prefixes: int | None = None
    ipv6_adoption: float | None = None
    collector: str = "APNIC DIX-IE"
    data_time: str = ""
    source: str = "APNIC THYME"

    @property
    def total(self) -> int:
        return self.ipv4 + self.ipv6


DEMO = Metrics(
    ipv4=1_074_549,
    ipv6=244_318,
    rpki_valid=942_576,
    rpki_unknown=374_745,
    rpki_invalid=1_546,
    path_avg_prepend=4.74,
    top_asn=16509,
    top_name="AMAZON-02",
    top_prefixes=15_642,
    top6_asn=9808,
    top6_name="CHINAMOBILE-CN",
    top6_prefixes=7_178,
    ipv6_adoption=43.36,
    collector="APNIC DIX-IE",
    data_time="DEMO",
    source="DEMO DATA",
)


# ---------------------------------------------------------------------------
# Tiny decorative raster engine — NO textual content is rendered here.
# ---------------------------------------------------------------------------

def blank():
    return [BLACK] * (W * H)


def px(buf, x, y, c):
    if 0 <= x < W and 0 <= y < H:
        buf[y * W + x] = c


def rect(buf, x, y, w, h, c):
    x0, x1 = max(0, x), min(W, x + w)
    y0, y1 = max(0, y), min(H, y + h)
    for yy in range(y0, y1):
        off = yy * W
        for xx in range(x0, x1):
            buf[off + xx] = c


def line(buf, x0, y0, x1, y1, c):
    dx = abs(x1 - x0)
    sx = 1 if x0 < x1 else -1
    dy = -abs(y1 - y0)
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        px(buf, x0, y0, c)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def glow_dot(buf, x, y, c):
    px(buf, x, y, c)
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        if 0 <= x + dx < W and 0 <= y + dy < H:
            px(buf, x + dx, y + dy, tuple(v // 3 for v in c))


def png(pixels) -> bytes:
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        base = y * W
        for x in range(W):
            r, g, b = pixels[base + x]
            raw += bytes((r, g, b, 255))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


# ---------------------------------------------------------------------------
# Native BUSY text helpers
# ---------------------------------------------------------------------------

def native_text(value, x, y, *, font="small", color=C_WHITE, align="top_left",
                width=None, scroll_rate=None, scroll_start_delay=None,
                scroll_repeat_delay=None):
    d = {
	"id": "native_text",
        "type": "text",
        "text": str(value),
        "x": x,
        "y": y,
        "font": font,
        "color": color,
        "align": align,
    }
    if width is not None:
        d["width"] = width
    if scroll_rate is not None:
        d["scroll_rate"] = scroll_rate
    if scroll_start_delay is not None:
        d["scroll_start_delay"] = scroll_start_delay
    if scroll_repeat_delay is not None:
        d["scroll_repeat_delay"] = scroll_repeat_delay
    return d


# ---------------------------------------------------------------------------
# BUSY Bar HTTP transport
# ---------------------------------------------------------------------------
class Busy:
    def __init__(self, host: str):
        host = host.replace("http://", "").replace("https://", "").rstrip("/")
        self.base = "http://" + host
        self.frame = 0
        self.ring = 4
        self.text_slots = 6
        self._last_text_slots = [None] * self.text_slots

    def _req(self, method, path, body=None, content_type=None, timeout=7):
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.getcode(), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def show(self, pixels, texts):
        """Draw one decorative image plus native BUSY text elements.

        The same fixed text IDs are emitted every frame, including empty slots,
        so stale text from the previous card cannot remain on screen.
        """
        fn = f"frame{self.frame % self.ring}.png"
        self.frame += 1
        qs = urllib.parse.urlencode({"application_name": APP, "file": fn})
        status, _ = self._req("POST", "/api/assets/upload?" + qs, png(pixels), "application/octet-stream")
        if status == 508:
            return
        if status not in (200, 201, 204):
            raise RuntimeError(f"asset upload HTTP {status}")

        elements = [{"id": "bg", "type": "image", "path": fn, "x": 0, "y": 0}]
        texts = list(texts[:self.text_slots])
        while len(texts) < self.text_slots:
            texts.append(native_text("", 0, 0, color="#00000000"))

        # Native text elements persist on the BUSY Bar until replaced.  Only
        # resend a slot when its contents/properties actually change.  Besides
        # reducing traffic, this is essential for scrolling text: repeatedly
        # drawing the same text element every animation frame restarts the
        # firmware's scroll animation before it has a chance to move.
        for i, item in enumerate(texts):
            sig = json.dumps(item, sort_keys=True, separators=(",", ":"))
            if sig == self._last_text_slots[i]:
                continue
            self._last_text_slots[i] = sig
            el = dict(item)
            el["id"] = f"txt{i}"
            elements.append(el)

        body = json.dumps({
            "application_name": APP,
            "priority": 30,
            "elements": elements,
        }).encode()
        status, _ = self._req("POST", "/api/display/draw", body, "application/json")
        if status not in (200, 201, 204, 409):
            raise RuntimeError(f"draw HTTP {status}")

    def clear(self):
        qs = urllib.parse.urlencode({"application_name": APP})
        try:
            self._req("DELETE", "/api/display/draw?" + qs)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Visual scenes
# ---------------------------------------------------------------------------
class Visualizer:
    def __init__(self):
        rng = random.Random(42)
        self.nodes = [(rng.randrange(W), rng.randrange(H), rng.choice([-1, 1])) for _ in range(10)]
        self.history4 = [0.42, .48, .46, .55, .61, .58, .66, .63, .70, .73, .71, .77, .79]
        self.history6 = [.28, .31, .35, .34, .41, .46, .48, .53, .51, .59, .63, .68, .72]

    def _bg_grid(self, b, phase, color=DARK_BLUE):
        for x in range(-phase % 8, W, 8):
            for y in (2, 8, 14):
                px(b, x, y, color)
        for y in range(H):
            px(b, int((y * 7 + phase * 2) % W), y, color)

    def number_card(self, t, label, value, color, text_color, history, icon):
        b = blank()
        self._bg_grid(b, int(t * 5), tuple(v // 4 for v in color))
        rect(b, 0, 0, 12, 16, tuple(max(0, v // 7) for v in color))
        # Pixel icon only; textual label/value are native BUSY elements.
        if icon == "4":
            rect(b, 3, 4, 2, 6, color); rect(b, 7, 3, 2, 10, color); rect(b, 3, 9, 6, 2, color)
        else:
            rect(b, 3, 4, 6, 2, color); rect(b, 3, 4, 2, 8, color); rect(b, 3, 8, 6, 2, color); rect(b, 7, 8, 2, 5, color); rect(b, 3, 11, 6, 2, color)
        texts = [
            native_text(label, 15, -1, font="small", color=C_DIM),
            native_text(f"{value:,}", 15, 6, font="normal", color=text_color),
        ]
        return b, texts

    def total(self, t, m):
        b = blank()
        cx, cy = 9, 8
        for yy in range(2, 15):
            for xx in range(2, 17):
                dx, dy = xx - cx, yy - cy
                d2 = dx * dx + (dy * 1.18) ** 2
                if 31 <= d2 <= 43:
                    px(b, xx, yy, CYAN)
        for yy in (5, 8, 11):
            line(b, 3, yy, 15, yy, DARK_BLUE)
        line(b, 9, 2, 9, 14, DARK_BLUE)
        glow_dot(b, 9 + int(5 * math.sin(t * 1.8)), 8, CYAN)
        for k in range(4):
            x = 20 + ((int(t * 11) + k * 15) % 52)
            px(b, x, 15, [BLUE, CYAN, VIOLET, MAGENTA][k])
        texts = [
            native_text("FRT SIZE", 20, -1, font="small", color=C_DIM),
            native_text(f"{m.total:,}", 20, 6, font="normal", color=C_WHITE),
        ]
        return b, texts

    def ipv6_adoption(self, t, m):
        b = blank()

        # Adoption is shown as a radial "signal" motif rather than another
        # globe/FRT counter. Concentric arcs pulse outwards from a small IPv6
        # node, suggesting reach/coverage across the Internet.
        cx, cy = 8, 8
        pulse = int(t * 5) % 12
        for r, col in ((2, CYAN), (4, VIOLET), (6, DARK_BLUE)):
            rr = r + (1 if pulse in (r, r + 1) else 0)
            for a in range(-60, 61, 10):
                rad = math.radians(a)
                x = cx + int(round(math.cos(rad) * rr))
                y = cy + int(round(math.sin(rad) * rr * 0.70))
                px(b, x, y, col)
        glow_dot(b, cx - 2, cy, CYAN)

        value = m.ipv6_adoption
        if value is not None:
            # A compact vertical gauge on the far right keeps this screen
            # visually distinct from the horizontal FRT-size animation.
            filled = max(0, min(12, int(round(12 * value / 100.0))))
            for i in range(12):
                y = 14 - i
                px(b, 70, y, VIOLET if i < filled else DARK_BLUE)
                px(b, 71, y, VIOLET if i < filled else DARK_BLUE)
            value_text = f"{value:.1f}%"
        else:
            value_text = "N/A"

        texts = [
            native_text("IPV6 ADOPTION", 18, -1, font="small", color=C_VIOLET),
            native_text(value_text, 18, 6, font="normal", color=C_WHITE),
        ]
        return b, texts

    def rpki(self, t, m, focus):
        vals = [m.rpki_valid, m.rpki_unknown, m.rpki_invalid]
        cols = [GREEN, AMBER, RED]
        ctexts = [C_GREEN, C_AMBER, C_RED]
        darks = [DARK_GREEN, DARK_AMBER, DARK_RED]
        labels = ["RPKI VALID", "RPKI UNKNOWN", "RPKI INVALID"]
        value = vals[focus]
        b = blank()

        # Shield motif kept far right/top so it never collides with the percentage.
        for yy in range(0, 7):
            span = max(0, 4 - abs(3 - yy))
            if span:
                px(b, 65 - span, yy, darks[focus])
                px(b, 65 + span, yy, darks[focus])
        glow_dot(b, 65, 3, cols[focus])

        if all(v is not None for v in vals) and sum(vals) > 0:
            total = sum(vals)
            x = 0
            for v, c in zip(vals, cols):
                w = int(round(W * v / total))
                rect(b, x, 15, w, 1, c)
                x += w
        else:
            for x in range(W):
                if (x + int(t * 8)) % 7 < 3:
                    px(b, x, 15, DIM)

        texts = [native_text(labels[focus], 1, -1, font="small", color=ctexts[focus])]
        if value is None or m.total <= 0:
            texts.append(native_text("N/A", 1, 7, font="normal", color=C_DIM))
        else:
            pct = 100.0 * value / m.total
            # Separate, native-font fields. Percentage gets its own right-aligned
            # space rather than sharing a hand-drawn 3x5 row with the count.
            texts.extend([
                native_text(f"{value:,}", 1, 7, font="small", color=C_WHITE),
                native_text(f"{pct:.1f}%", 72, 7, font="small", color=ctexts[focus], align="top_right"),
            ])
        return b, texts

    def path(self, t, m):
        b = blank()
        xs = [46, 51, 56, 61, 66, 70]
        node_cols = [CYAN, BLUE, BLUE, BLUE, VIOLET, MAGENTA]
        for i in range(len(xs) - 1):
            line(b, xs[i], 10, xs[i + 1], 10, DIM)
        pulse = int(t * 5) % len(xs)
        for i, x in enumerate(xs):
            c = (255, 255, 255) if i == pulse else node_cols[i]
            rect(b, x - 1, 8, 3, 5, tuple(v // 3 for v in c))
            px(b, x, 10, c)
        value = "N/A" if m.path_avg_prepend is None else f"{m.path_avg_prepend:.2f}"
        texts = [
            native_text("AS PATH AVG", 1, -1, font="small", color=C_VIOLET),
            native_text(value, 1, 7, font="normal", color=C_WHITE),
            native_text("HOPS", 26, 8, font="tiny", color=C_DIM),
        ]
        return b, texts

    def top_origin(self, t, m, version=4):
        b = blank()
        # crown pixel-art only
        rect(b, 1, 2, 9, 2, DARK_AMBER)
        for x, y in ((1, 1), (5, 0), (9, 1)):
            px(b, x, y, AMBER)

        if version == 6:
            asn_v, name_v, pfx_v = m.top6_asn, m.top6_name, m.top6_prefixes
            title = "TOP ORIGIN V6"
            accent = C_VIOLET
        else:
            asn_v, name_v, pfx_v = m.top_asn, m.top_name, m.top_prefixes
            title = "TOP ORIGIN V4"
            accent = C_AMBER

        asn = "AS?" if asn_v is None else f"AS{asn_v}"
        pfx = "?" if pfx_v is None else f"{pfx_v:,}"
        # Two-line layout: title + one high-contrast native text ticker.
        # BUSY Bar scrolls the second line only when it exceeds the available width.
        name = name_v or "UNKNOWN"
        if name.upper().startswith("AS-"):
            named_as = name
        else:
            named_as = f"AS-{name}"
        scroll = f"{pfx} pfx - {named_as} - {asn}"
        texts = [
            native_text(title, 13, -1, font="small", color=accent),
            native_text(scroll, 0, 8, font="small", color=C_WHITE, width=72,
                        scroll_rate=360, scroll_start_delay=500, scroll_repeat_delay=900),
        ]
        return b, texts


SCENES = [
    "ipv4",
    "ipv6",
    "total",
    "rpki_valid",
    "rpki_unknown",
    "rpki_invalid",
    "path_prepend",
    "top_origin_v4",
    "top_origin_v6",
    "ipv6_adoption",
]


def _approx_text_width(text: str, font="small") -> int:
    """Conservative BUSY bitmap-font width estimate used only for dwell timing."""
    # The native small font is roughly 4 px/character on this display.
    return len(text) * (4 if font == "small" else 3)


def _top_origin_scroll_text(m: Metrics, version: int) -> str:
    if version == 6:
        asn_v, name_v, pfx_v = m.top6_asn, m.top6_name, m.top6_prefixes
    else:
        asn_v, name_v, pfx_v = m.top_asn, m.top_name, m.top_prefixes
    asn = "AS?" if asn_v is None else f"AS{asn_v}"
    pfx = "?" if pfx_v is None else f"{pfx_v:,}"
    name = name_v or "UNKNOWN"
    if name.upper().startswith("AS-"):
        named_as = name
    else:
        named_as = f"AS-{name}"
    return f"{pfx} pfx - {named_as} - {asn}"


def scene_duration(name: str, m: Metrics, speed: float) -> float:
    """Return screen dwell time; Top Origin cards remain for 2x --speed."""
    base = max(0.5, speed)
    if name in ("top_origin_v4", "top_origin_v6"):
        return base * 2.0
    return base


def render_scene_index(v: Visualizer, m: Metrics, index: int, t: float):
    name = SCENES[index % len(SCENES)]
    if name == "ipv4":
        return (*v.number_card(t, "IPV4 PREFIXES", m.ipv4, BLUE, C_BLUE, v.history4, "4"), name)
    if name == "ipv6":
        return (*v.number_card(t, "IPV6 PREFIXES", m.ipv6, VIOLET, C_VIOLET, v.history6, "6"), name)
    if name == "ipv6_adoption":
        return (*v.ipv6_adoption(t, m), name)
    if name == "total":
        return (*v.total(t, m), name)
    if name == "rpki_valid":
        return (*v.rpki(t, m, 0), name)
    if name == "rpki_unknown":
        return (*v.rpki(t, m, 1), name)
    if name == "rpki_invalid":
        return (*v.rpki(t, m, 2), name)
    if name == "path_prepend":
        return (*v.path(t, m), name)
    if name == "top_origin_v4":
        return (*v.top_origin(t, m, 4), name)
    return (*v.top_origin(t, m, 6), name)


# ---------------------------------------------------------------------------
# Lightweight APNIC/Thyme data source
# ---------------------------------------------------------------------------

def fetch_text(url: str, *, max_bytes=256_000, timeout=12, range_bytes=None):
    headers = {"User-Agent": "busybar-frt-ticker/0.2"}
    if range_bytes:
        headers["Range"] = f"bytes=0-{range_bytes - 1}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(max_bytes).decode("utf-8", "replace")


def _extract_int(text: str, label: str) -> int:
    m = re.search(r"^\s*" + re.escape(label) + r"\s*:\s*([0-9,]+)\s*$", text, re.MULTILINE | re.IGNORECASE)
    if not m:
        raise RuntimeError(f"APNIC summary field not found: {label}")
    return int(m.group(1).replace(",", ""))


def _extract_float(text: str, pattern: str) -> float:
    m = re.search(pattern + r"\s*:\s*([0-9]+(?:\.[0-9]+)?)", text, re.MULTILINE | re.IGNORECASE)
    if not m:
        raise RuntimeError(f"APNIC summary field not found: {pattern}")
    return float(m.group(1))


def parse_v4_summary(text: str):
    return {
        "prefixes": _extract_int(text, "BGP routing table entries examined"),
        "valid": _extract_int(text, "Number of IPv4 prefixes with a valid ROA"),
        "invalid": _extract_int(text, "Number of IPv4 prefixes with an invalid ROA"),
        "unknown": _extract_int(text, "Number of IPv4 prefixes with no ROA"),
        "path_avg": _extract_float(text, r"Average AS path length visible in the Internet Routing Table"),
    }


def parse_v6_summary(text: str):
    return {
        "prefixes": _extract_int(text, "BGP routing table entries examined"),
        "valid": _extract_int(text, "Number of IPv6 prefixes with a valid ROA"),
        "invalid": _extract_int(text, "Number of IPv6 prefixes with an invalid ROA"),
        "unknown": _extract_int(text, "Number of IPv6 prefixes with no ROA"),
        "path_avg": _extract_float(text, r"Average AS path length"),
    }


def parse_top_origin_v4(text: str):
    """Parse the first data row of APNIC's global IPv4 per-AS prefix league."""
    for raw in text.splitlines():
        # ASN, No of nets, /20 equiv, MaxAgg, Description
        m = re.match(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(.+?)\s*$", raw)
        if not m:
            continue
        asn = int(m.group(1))
        prefixes = int(m.group(2))
        desc = re.sub(r"\s+", " ", m.group(5)).strip()
        if asn > 0 and prefixes > 0:
            return asn, prefixes, desc
    return None, None, ""


def parse_top_origin_v6(text: str):
    """Parse the first data row of APNIC's global IPv6 per-AS prefix league."""
    for raw in text.splitlines():
        m = re.match(r"^\s*(\d+)\s+(\d+)\s+(.+?)\s*$", raw)
        if not m:
            continue
        asn = int(m.group(1))
        prefixes = int(m.group(2))
        desc = re.sub(r"\s+", " ", m.group(3)).strip()
        if asn > 0 and prefixes > 0:
            return asn, prefixes, desc
    return None, None, ""


def parse_ipv6_adoption(text: str) -> float:
    """Return Google's latest worldwide Total IPv6 adoption percentage."""
    # The public statistics page includes a label like:
    #   Total IPv6: 47.99%
    # Be permissive about whitespace / HTML between the label and number.
    m = re.search(r"Total\s*IPv6\s*:?[^0-9]{0,80}([0-9]+(?:\.[0-9]+)?)\s*%",
                  text, re.IGNORECASE | re.DOTALL)
    if not m:
        raise RuntimeError("Google IPv6 adoption field not found")
    value = float(m.group(1))
    if not 0.0 <= value <= 100.0:
        raise RuntimeError(f"Google IPv6 adoption out of range: {value}")
    return value


def fetch_live_metrics(progress=None):
    if progress:
        progress("IPV4", 0)
    v4 = parse_v4_summary(fetch_text(APNIC_V4_SUMMARY))
    if progress:
        progress("IPV6", 0)
    v6 = parse_v6_summary(fetch_text(APNIC_V6_SUMMARY))

    if progress:
        progress("IPV6 ADOPTION", 0)
    adoption = parse_ipv6_adoption(
        fetch_text(GOOGLE_IPV6_STATS, max_bytes=512_000, timeout=12)
    )

    # This endpoint is several MB in full, but it is sorted. Read only its first
    # 64 KiB; that contains the heading and the top rows. A Range header is sent
    # when supported, and read() is capped even if the server ignores Range.
    if progress:
        progress("TOP AS", 0)
    top_chunk = fetch_text(APNIC_TOP_ORIGIN_V4, max_bytes=65_536, range_bytes=65_536)
    top_asn, top_pfx, top_name = parse_top_origin_v4(top_chunk)
    top6_chunk = fetch_text(APNIC_TOP_ORIGIN_V6, max_bytes=16_384, range_bytes=16_384)
    top6_asn, top6_pfx, top6_name = parse_top_origin_v6(top6_chunk)

    total = v4["prefixes"] + v6["prefixes"]
    path_avg = None
    if total:
        path_avg = ((v4["path_avg"] * v4["prefixes"]) +
                    (v6["path_avg"] * v6["prefixes"])) / total

    return Metrics(
        ipv4=v4["prefixes"],
        ipv6=v6["prefixes"],
        rpki_valid=v4["valid"] + v6["valid"],
        rpki_unknown=v4["unknown"] + v6["unknown"],
        rpki_invalid=v4["invalid"] + v6["invalid"],
        path_avg_prepend=path_avg,
        top_asn=top_asn,
        top_name=top_name,
        top_prefixes=top_pfx,
        top6_asn=top6_asn,
        top6_name=top6_name,
        top6_prefixes=top6_pfx,
        ipv6_adoption=adoption,
        collector="APNIC DIX-IE",
        data_time=time.strftime("%Y-%m-%d"),
        source="APNIC THYME",
    )


# ---------------------------------------------------------------------------
# Cache / background loader
# ---------------------------------------------------------------------------

def cache_path():
    return Path(__file__).resolve().parent / CACHE_NAME


def save_cache(m: Metrics):
    try:
        cache_path().write_text(json.dumps(dataclasses.asdict(m), indent=2))
    except OSError:
        pass


def load_cache(max_age_minutes=90):
    p = cache_path()
    try:
        if not p.exists() or time.time() - p.stat().st_mtime > max_age_minutes * 60:
            return None
        data = json.loads(p.read_text())
        # Ignore caches from the old MRT-based prototype.
        if "path_avg_unique" in data:
            data.pop("path_avg_unique", None)
            data.pop("peer_ip", None)
            data.pop("peer_asn", None)
        return Metrics(**{k: v for k, v in data.items() if k in Metrics.__dataclass_fields__})
    except Exception:
        return None


class LiveLoader:
    """Fetch fresh live data in the background; cache is fallback only."""
    def __init__(self):
        self.metrics = None
        self.cached_metrics = load_cache(max_age_minutes=24 * 60)
        self.error = None
        self.stage = "START"
        self.count = 0
        self.done = False
        self.refreshing = False
        self.generation = 0
        self.last_success_monotonic = None
        self.last_attempt_monotonic = None
        self.thread = None
        self._lock = threading.Lock()

    def progress(self, stage, count):
        with self._lock:
            self.stage, self.count = stage, count

    def start(self):
        """Always fetch fresh data at startup."""
        self.refresh(initial=True)

    def refresh(self, initial=False):
        with self._lock:
            if self.refreshing:
                return False
            self.refreshing = True
            self.error = None
            self.last_attempt_monotonic = time.monotonic()
            if initial:
                self.done = False
                self.stage = "START"
        self.thread = threading.Thread(target=self._run, args=(initial,), daemon=True)
        self.thread.start()
        return True

    def _run(self, initial):
        try:
            fresh = fetch_live_metrics(self.progress)
            save_cache(fresh)
            with self._lock:
                self.metrics = fresh
                self.cached_metrics = fresh
                self.stage = "READY"
                self.done = True
                self.generation += 1
                self.last_success_monotonic = time.monotonic()
        except Exception as e:
            with self._lock:
                self.error = str(e)
                self.stage = "ERROR"
                self.done = True
                # On startup only, fall back to cache if available.
                if initial and self.metrics is None and self.cached_metrics is not None:
                    self.metrics = self.cached_metrics
                    self.generation += 1
        finally:
            with self._lock:
                self.refreshing = False


def loading_scene(stage: str, t: float, error: str | None = None):
    b = blank()
    color = RED if error else CYAN
    for x in range(W):
        if (x + int(t * 18)) % 12 < 5:
            px(b, x, 15, color if not error else DARK_RED)
    texts = [
        native_text("DATA ERROR" if error else "SYNC BGP STATS", 36, -1,
                    font="small", color=C_RED if error else C_CYAN, align="top_mid"),
    ]
    if error:
        texts.append(native_text(error.upper(), 0, 8, font="tiny", color=C_DIM, width=72,
                                 scroll_rate=420, scroll_start_delay=250, scroll_repeat_delay=700))
    else:
        texts.append(native_text(stage[:12], 1, 7, font="small", color=C_WHITE))
    return b, texts


# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Full Routing Table ticker for BUSY Bar")
    p.add_argument("--host", default=DEFAULT_HOST)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="use representative demo data instead of live statistics")
    mode.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--speed", type=float, default=4.5,
                   help="seconds each screen remains visible (default: 4.5)")
    p.add_argument("--fps", type=float, default=8.0,
                   help="decorative animation frames per second (default: 8)")
    p.add_argument("--autorefresh", type=float, default=60.0, metavar="MINUTES",
                   help="live-data refresh interval in minutes; 0 disables (default: 60)")
    p.add_argument("--once", action="store_true", help="render one frame and exit")
    return p.parse_args()


def main():
    args = parse_args()
    demo = args.demo
    viz = Visualizer()
    speed = max(0.5, args.speed)
    autorefresh_minutes = max(0.0, args.autorefresh)
    autorefresh_seconds = autorefresh_minutes * 60.0

    loader = None
    m = DEMO
    if not demo:
        loader = LiveLoader()
        loader.start()  # startup is always a fresh network fetch

    bb = Busy(args.host)
    fps = max(1.0, min(15.0, args.fps))
    frame_dt = 1.0 / fps
    app_t0 = time.monotonic()

    # Scene timing is independent from application uptime. This is important
    # after the initial sync: the first completed-data card must always be IPv4.
    scene_index = 0
    scene_started = None
    cycle_generation = -1

    print(f"FRT Ticker -> {bb.base}  mode={'DEMO' if demo else 'LIVE'}  speed={speed:g}s  (Ctrl-C to stop)")
    if not demo:
        ar = "off" if autorefresh_minutes == 0 else f"{autorefresh_minutes:g} min"
        print(f"source: APNIC/Thyme summaries  autorefresh={ar}")

    try:
        while True:
            start = time.monotonic()
            app_elapsed = start - app_t0

            if loader is not None:
                # Schedule background refreshes from the last successful fetch.
                if (autorefresh_seconds > 0 and loader.last_attempt_monotonic is not None
                        and not loader.refreshing
                        and start - loader.last_attempt_monotonic >= autorefresh_seconds):
                    loader.refresh(initial=False)

                # Initial synchronization owns the screen until it completes.
                if not loader.done:
                    frame, texts = loading_scene(loader.stage, app_elapsed)
                elif loader.metrics is None:
                    frame, texts = loading_scene(loader.stage, app_elapsed, loader.error)
                else:
                    m = loader.metrics
                    # On the first usable dataset, explicitly start from IPv4.
                    # Later autorefresh generations update values in-place without
                    # disturbing whichever screen the user is currently reading.
                    if scene_started is None:
                        scene_index = 0
                        scene_started = start
                        cycle_generation = loader.generation
                    t_scene = start - scene_started
                    name = SCENES[scene_index]
                    dwell = scene_duration(name, m, speed)
                    if t_scene >= dwell:
                        scene_index = (scene_index + 1) % len(SCENES)
                        scene_started = start
                        t_scene = 0.0
                    frame, texts, _ = render_scene_index(viz, m, scene_index, t_scene)
            else:
                if scene_started is None:
                    scene_started = start
                t_scene = start - scene_started
                name = SCENES[scene_index]
                dwell = scene_duration(name, m, speed)
                if t_scene >= dwell:
                    scene_index = (scene_index + 1) % len(SCENES)
                    scene_started = start
                    t_scene = 0.0
                frame, texts, _ = render_scene_index(viz, m, scene_index, t_scene)

            try:
                bb.show(frame, texts)
            except (urllib.error.URLError, RuntimeError) as e:
                print(f"display: {e}", file=sys.stderr)

            if args.once:
                break
            dt = time.monotonic() - start
            if dt < frame_dt:
                time.sleep(frame_dt - dt)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        bb.clear()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Procedural lava lamp for BUSY Bar.

A purely ambient 72x16 animation: soft lava blobs rise, fall, merge through
continuous surface tension, split smoothly, and gently drift sideways. Everything is generated locally; no dependencies.

Examples:
    python3 lava_lamp.py
    python3 lava_lamp.py --host 127.0.0.1:8080
    python3 lava_lamp.py --palette classic --speed 0.8 --blobs 7
    python3 lava_lamp.py --lava '#FF5500' --background '#120018'
    python3 lava_lamp.py --density 1.4 --wobble 1.3 --fps 12
    python3 lava_lamp.py --wall-hit              # visible bounce at top/bottom
    python3 lava_lamp.py --seed 42 --test
"""

import argparse
import colorsys
import copy
import json
import math
import random
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

APP = "lava-lamp"
W, H = 72, 16

PALETTES = {
    "classic": ("#FF5A24", "#FFB000", "#190019"),
    "magenta": ("#FF2D95", "#9B4DFF", "#120018"),
    "ocean":   ("#00D4FF", "#0077FF", "#001226"),
    "toxic":   ("#7CFF00", "#D7FF2F", "#061400"),
    "ice":     ("#D8FFFF", "#5DEBFF", "#00141A"),
    "sunset":  ("#FF3A5E", "#FFB13B", "#21000B"),
}


def parse_args():
    p = argparse.ArgumentParser(description="Procedural lava lamp for BUSY Bar")
    p.add_argument("--host", default="10.0.4.20")
    p.add_argument("--fps", type=float, default=12.0, help="frames per second (default: 12)")
    p.add_argument("--speed", type=float, default=8.0, help="vertical motion speed multiplier (default: 8)")
    p.add_argument("--blobs", type=int, default=6, help="number of lava blobs (default: 6)")
    p.add_argument("--min-radius", type=float, default=5.0, help="minimum blob radius in pixels (default: 5)")
    p.add_argument("--max-radius", type=float, default=7.2, help="maximum blob radius in pixels (default: 7.2)")
    p.add_argument("--wobble", type=float, default=1.0, help="horizontal drift amplitude")
    p.add_argument("--density", type=float, default=1.0, help="metaball density / blob fullness")
    p.add_argument("--interaction", type=float, default=1.0,
                   help="blob-to-blob attraction/pressure strength (default: 1.0)")
    p.add_argument("--spread", type=float, default=1.0,
                   help="strength of lateral distribution across the lamp (default: 1.0)")
    p.add_argument("--split-radius", type=float, default=8.5,
                   help="merged blobs larger than this radius become unstable and split (default: 8.5)")
    p.add_argument("--split-delay", type=float, default=1.8,
                   help="seconds an oversized blob must persist before splitting (default: 1.8)")
    p.add_argument("--no-split", action="store_true",
                   help="disable automatic breakup of oversized merged blobs")
    p.add_argument("--softness", type=float, default=0.65, help="edge softness, 0=hard, 1=very soft")
    p.add_argument("--pulse", type=float, default=0.16, help="blob breathing amount")
    p.add_argument("--palette", choices=sorted(PALETTES), default="classic")
    p.add_argument("--lava", default=None, help="primary lava color, #RRGGBB")
    p.add_argument("--highlight", default=None, help="secondary/highlight color, #RRGGBB")
    p.add_argument("--background", default=None, help="background color, #RRGGBB")
    p.add_argument("--brightness", type=float, default=1.0, help="overall lava brightness")
    p.add_argument("--seed", type=int, default=None, help="deterministic animation seed")
    p.add_argument("--reverse", action="store_true", help="invert the initial rise/fall bias")
    p.add_argument("--wall-hit", action="store_true",
                   help="make blobs reverse visibly at the top/bottom edges; default turns around off-screen")
    p.add_argument("--no-glow", action="store_true", help="disable soft halo around lava")
    p.add_argument("--test", action="store_true", help="draw one frame and exit")
    return p.parse_args()


def _base(host):
    host = host.replace("http://", "").replace("https://", "").rstrip("/")
    return "http://" + host


def _hex(s):
    s = s.strip().lstrip("#")
    if len(s) != 6:
        raise ValueError("color must be #RRGGBB")
    return tuple(int(s[i:i+2], 16) for i in (0, 2, 4))


def _mix(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t + 0.5) for i in range(3))


def _scale(c, k):
    return tuple(max(0, min(255, int(v * k + 0.5))) for v in c)


def _png(pixels):
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        base = y * W
        for x in range(W):
            r, g, b = pixels[base + x]
            raw += bytes((r, g, b, 255))

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 1))
            + chunk(b"IEND", b""))


_RING = 4
_frame_no = 0


def _post(host, path, data, content_type):
    req = urllib.request.Request(_base(host) + path, data=data, method="POST",
                                 headers={"Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.getcode()


def show(host, pixels):
    global _frame_no
    fn = "frame%d.png" % (_frame_no % _RING)
    _frame_no += 1
    try:
        _post(host, "/api/assets/upload?application_name=%s&file=%s" % (APP, fn),
              _png(pixels), "application/octet-stream")
        body = {"application_name": APP,
                "elements": [{"id": "frame", "type": "image", "path": fn, "x": 0, "y": 0}]}
        return _post(host, "/api/display/draw", json.dumps(body).encode(), "application/json")
    except urllib.error.HTTPError as e:
        return e.code
    except urllib.error.URLError as e:
        raise RuntimeError(f"push failed: {e}") from e


def clear(host):
    qs = urllib.parse.urlencode({"application_name": APP})
    req = urllib.request.Request(_base(host) + "/api/display/draw?" + qs, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        pass


class Blob:
    """One simulated lump of wax.

    Motion is integrated from forces rather than sampled from a periodic curve.
    A blob is heated below the visible display, becomes buoyant and rises; it
    cools above the display, becomes heavy and falls. Horizontal position is
    governed by inertia, a very weak lane-restoring force and interactions with
    neighbouring blobs. The result is intentionally aperiodic.
    """

    def __init__(self, rng, args, index, x0):
        self.uid = index
        self.radius = rng.uniform(args.min_radius, args.max_radius)
        self.mass = max(0.5, self.radius * self.radius)
        # Visual size is deliberately decoupled from physical radius. Merge/split
        # can change physical area instantly for conservation, but the visible
        # contour eases toward that new size so it never pops between frames.
        self.visual_radius = self.radius
        self.visual_strength = 1.0
        self.x = x0
        self.y = rng.uniform(-3.0, H + 3.0)
        self.vx = rng.uniform(-0.32, 0.32)
        motion = max(0.05, args.speed / 8.0)
        self.vy = rng.uniform(-1.0, 1.0) * motion
        # Temperature and motion start consistent with one another so a blob
        # never gets stranded mid-lamp with near-zero buoyancy.
        self.heat = rng.uniform(0.55, 0.95) if self.vy < 0 else rng.uniform(-0.95, -0.55)
        if args.reverse:
            self.vy *= -1.0
            self.heat *= -1.0
        self.anchor_x = x0
        self.phase = rng.random() * math.tau
        self.phase2 = rng.random() * math.tau
        self.shape_phase = rng.random() * math.tau
        self.shape_rate = rng.uniform(0.12, 0.28)
        self.spin = rng.choice((-1.0, 1.0)) * rng.uniform(0.08, 0.22)
        self.drift_phase = rng.random() * math.tau
        self.drift_rate = rng.uniform(0.035, 0.09)
        self.drift_bias = rng.uniform(-0.10, 0.10)
        self.contact = 0.0
        self.contact_dx = 0.0
        self.contact_dy = 0.0
        self.merge_cooldown = 0.0
        self.split_hold = 0.0
        self.age = 0.0

        # Render state, updated by the physics step.
        self.rx = self.radius
        self.ry = self.radius
        self.lobe_w = 0.24
        self.lobe_dx = 0.0
        self.lobe_dy = 0.0
        self.neighbor_pull_x = 0.0
        self.neighbor_pull_y = 0.0

    def render_state(self):
        return {
            "x": self.x, "y": self.y,
            "rx": max(0.8, self.rx), "ry": max(0.8, self.ry),
            "strength": max(0.0, min(1.0, self.visual_strength)),
            "vy": self.vy,
            "lobe_w": self.lobe_w,
            "lobe_dx": self.lobe_dx,
            "lobe_dy": self.lobe_dy,
        }


def _initial_x_positions(rng, args):
    """Stratified X positions: well spread, but not visibly on a rigid grid."""
    n = args.blobs
    usable_l = max(3.0, args.max_radius * 0.6)
    usable_r = W - usable_l
    cell = (usable_r - usable_l) / max(1, n)
    xs = []
    for i in range(n):
        center = usable_l + (i + 0.5) * cell
        jitter = rng.uniform(-0.28, 0.28) * cell
        xs.append(max(1.0, min(W - 2.0, center + jitter)))
    rng.shuffle(xs)
    return xs



def _coalesce_one(blobs, args):
    """Physically merge one strongly-overlapping pair, conserving wax area.

    The metaball renderer already makes first contact look liquid. Once the two
    centres have spent enough time deep inside the same contour, this function
    turns them into one simulated mass. At most one pair is merged per substep
    so the transition remains stable and organic.
    """
    if len(blobs) < 2 or args.interaction <= 0.0:
        return False
    motion = max(0.05, args.speed / 8.0)
    best = None
    threshold = max(args.min_radius * 1.25, args.split_radius)
    for i in range(len(blobs)):
        a = blobs[i]
        # Once a mass is already oversized, surface tension stops accepting
        # more wax and the breakup instability gets priority. Without this,
        # several blobs can collapse into one giant mass before it has time to
        # pinch apart.
        if a.merge_cooldown > 0.0 or a.radius >= threshold:
            continue
        for j in range(i + 1, len(blobs)):
            b = blobs[j]
            if b.merge_cooldown > 0.0 or b.radius >= threshold:
                continue
            dx, dy = b.x - a.x, b.y - a.y
            dist = math.hypot(dx, dy)
            surface = a.radius + b.radius
            rel_v = math.hypot(b.vx - a.vx, b.vy - a.vy)
            # Deep overlap + similar velocity means surface tension has had
            # enough time to turn the pair into one physical wax body.
            if dist > surface * 0.46 or rel_v > 2.0 * motion:
                continue
            score = dist / max(surface, 0.1) + rel_v * 0.08
            if best is None or score < best[0]:
                best = (score, i, j)
    if best is None:
        return False

    _, i, j = best
    a, b = blobs[i], blobs[j]
    area_a = a.radius * a.radius
    area_b = b.radius * b.radius
    total = area_a + area_b
    wa, wb = area_a / total, area_b / total
    a.x = a.x * wa + b.x * wb
    a.y = a.y * wa + b.y * wb
    a.vx = a.vx * wa + b.vx * wb
    a.vy = a.vy * wa + b.vy * wb
    a.heat = a.heat * wa + b.heat * wb
    a.anchor_x = a.anchor_x * wa + b.anchor_x * wb
    old_visual = math.sqrt(max(0.01, a.visual_radius * a.visual_radius + b.visual_radius * b.visual_radius))
    a.radius = math.sqrt(total)
    a.mass = total
    # Preserve apparent area at the instant of coalescence, then ease to the
    # exact physical radius over subsequent physics ticks.
    a.visual_radius = min(a.radius * 1.08, old_visual)
    a.visual_strength = 1.0
    a.phase = (a.phase * wa + b.phase * wb + 0.37) % math.tau
    a.phase2 = (a.phase2 * wa + b.phase2 * wb + 0.71) % math.tau
    a.shape_phase = (a.shape_phase * wa + b.shape_phase * wb + 0.53) % math.tau
    a.contact = 1.0
    a.merge_cooldown = 1.0
    a.split_hold = 0.0
    del blobs[j]
    return True


def _split_oversized(blobs, h, args):
    """Break one oversized wax body into two children with area conservation."""
    if args.no_split:
        return False
    threshold = max(args.min_radius * 1.25, args.split_radius)
    for b in blobs:
        b.age += h
        b.merge_cooldown = max(0.0, b.merge_cooldown - h)
        if b.radius <= threshold or b.merge_cooldown > 0.0:
            b.split_hold = max(0.0, b.split_hold - h * 0.7)
            continue
        b.split_hold += h
        if b.split_hold < args.split_delay:
            continue

        # Deterministic but non-repeating split ratio. Area (r^2) is conserved.
        phase = 0.5 + 0.5 * math.sin(b.shape_phase + b.age * 0.73)
        frac = 0.43 + 0.14 * phase
        total_area = b.radius * b.radius
        r1 = math.sqrt(total_area * frac)
        r2 = math.sqrt(total_area * (1.0 - frac))

        child = copy.copy(b)
        # Split mostly across X, with a small vertical component. This creates
        # the characteristic waist-then-separation of a lava-lamp blob.
        ang = math.sin(b.phase + b.age * 0.19) * 0.38
        nx, ny = math.cos(ang), math.sin(ang)
        sep = max(1.8, min(r1, r2) * 0.72)
        cx, cy = b.x, b.y
        base_vx, base_vy = b.vx, b.vy

        parent_visual = b.visual_radius
        b.radius = r1
        b.mass = r1 * r1
        # Do not snap the parent smaller. Keep the old silhouette briefly and
        # let it relax while the newborn lobe grows out of the waist.
        b.visual_radius = parent_visual
        b.visual_strength = 1.0
        b.x = cx - nx * sep * 0.5
        b.y = cy - ny * sep * 0.5
        b.vx = base_vx - nx * 0.34
        b.vy = base_vy - ny * 0.16
        b.anchor_x -= sep * 0.25
        b.phase = (b.phase + 0.91) % math.tau
        b.phase2 = (b.phase2 + 0.43) % math.tau
        b.shape_phase = (b.shape_phase + 0.67) % math.tau
        b.merge_cooldown = 2.8
        b.split_hold = 0.0
        b.contact = 0.0

        child.radius = r2
        child.mass = r2 * r2
        # New child fades/grows in instead of appearing at full radius in one
        # frame. This is the key to a liquid-looking pinch-off.
        child.visual_radius = max(1.0, r2 * 0.28)
        child.visual_strength = 0.22
        child.x = cx + nx * sep * 0.5
        child.y = cy + ny * sep * 0.5
        child.vx = base_vx + nx * 0.34
        child.vy = base_vy + ny * 0.16
        child.anchor_x += sep * 0.25
        child.phase = (child.phase + 2.17) % math.tau
        child.phase2 = (child.phase2 + 1.31) % math.tau
        child.shape_phase = (child.shape_phase + 1.73) % math.tau
        child.merge_cooldown = 2.8
        child.split_hold = 0.0
        child.contact = 0.0
        child.contact_dx = child.contact_dy = 0.0
        child.lobe_dx = child.lobe_dy = 0.0
        blobs.append(child)
        return True
    return False

# Pair-state for continuous coalescence / breakup.  Blobs are never deleted or
# resized when they merge visually; this avoids any discontinuity in the
# metaball field.  A pair simply becomes capillary-bonded for a while, then an
# oversized combined mass develops a neck and is pushed apart smoothly.
_PAIR_CONTACT = {}
_PAIR_SPLIT = {}
# For every active breakup, remember which member is driven upward. This
# deliberately restores counter-flow after capillary velocity matching has
# made a merged pair travel together.
_PAIR_SPLIT_DIR = {}


def _advance_physics(blobs, dt, t, args):
    """Advance the wax simulation with stable substeps.

    The thermal zones sit outside the visible 16px display unless --wall-hit is
    enabled. Pairwise forces combine mild cohesion with close-range pressure:
    blobs approach and visually merge, but do not collapse into one permanent
    point. A weak anchor force keeps the whole lamp filled across the X axis.
    """
    if dt <= 0:
        return

    interaction = max(0.0, args.interaction)
    spread = max(0.0, args.spread)
    # Stable even after a delayed frame / network hiccup.
    steps = max(1, min(6, int(math.ceil(dt / 0.025))))
    h = dt / steps

    for _ in range(steps):
        forces = [[0.0, 0.0] for _ in blobs]

        # Pair forces model surface tension. At medium range there is a weak
        # attraction; once two wax surfaces touch, capillary attraction becomes
        # stronger and their velocities start to match. This lets the metaballs
        # visibly coalesce into one larger mass instead of bouncing apart.
        for b in blobs:
            b.contact *= math.exp(-3.0 * h)
            b.contact_dx *= math.exp(-3.0 * h)
            b.contact_dy *= math.exp(-3.0 * h)

        for i in range(len(blobs)):
            a = blobs[i]
            for j in range(i + 1, len(blobs)):
                b = blobs[j]
                dx = b.x - a.x
                dy = b.y - a.y
                dist = math.hypot(dx, dy) + 1e-6
                surface = a.radius + b.radius
                influence = surface * 2.25
                if dist >= influence:
                    continue
                nx, ny = dx / dist, dy / dist

                key = (min(a.uid, b.uid), max(a.uid, b.uid))
                hold = _PAIR_CONTACT.get(key, 0.0)
                split_left = _PAIR_SPLIT.get(key, 0.0)
                split_dir = _PAIR_SPLIT_DIR.get(key, 0)

                if dist > surface:
                    # Gentle long-range cohesion: nearby blobs slowly wander
                    # toward one another. Contact memory decays smoothly.
                    q = (influence - dist) / max(influence - surface, 0.1)
                    f = interaction * 0.11 * q * q
                    sign = 1.0
                    hold = max(0.0, hold - h * 1.4)
                else:
                    overlap = max(0.0, 1.0 - dist / max(surface, 0.1))
                    hold += h * (0.45 + overlap)
                    combined_radius = math.sqrt(a.radius * a.radius + b.radius * b.radius)

                    # Once a visually merged mass has stayed oversized long
                    # enough, surface tension loses stability and a neck forms.
                    # Crucially, neither blob changes radius: breakup is only a
                    # progressive force, so there is no geometric pop.
                    if (not args.no_split and combined_radius >= args.split_radius
                            and hold >= args.split_delay and split_left <= 0.0):
                        split_left = 1.5
                        hold = 0.0
                        # A real lava lamp does not preserve one common vertical
                        # velocity forever after a large mass pinches apart. The
                        # two lobes can have different temperatures/densities, so
                        # one continues upward while the other starts sinking.
                        # Pick the hotter member for the upward branch when there
                        # is a meaningful difference; otherwise alternate by uid
                        # so the population never converges to one direction.
                        if abs(a.heat - b.heat) > 0.10:
                            split_dir = 1 if a.heat >= b.heat else -1
                        else:
                            split_dir = 1 if ((a.uid + b.uid) & 1) == 0 else -1
                        _PAIR_SPLIT_DIR[key] = split_dir

                    if split_left > 0.0:
                        phase = 1.0 - split_left / 1.5
                        # Ease-in/ease-out repulsion: weak at first while the
                        # waist thins, strongest in the middle, then fading.
                        pulse = math.sin(math.pi * max(0.0, min(1.0, phase)))
                        f = interaction * (0.30 + 1.05 * pulse)
                        sign = -1.0

                        # Restore counter-flow smoothly during the pinch-off.
                        # Capillary bonding above intentionally matches velocity;
                        # without this thermal bifurcation repeated merges make
                        # every blob eventually march in the same direction.
                        # split_dir=+1 means a becomes the warm/upward lobe and b
                        # the cool/downward lobe (screen Y grows downward).
                        if split_dir == 0:
                            split_dir = 1 if ((a.uid + b.uid) & 1) == 0 else -1
                            _PAIR_SPLIT_DIR[key] = split_dir
                        dir_a = split_dir
                        dir_b = -split_dir
                        thermal_k = min(1.0, h * (0.55 + 1.20 * pulse))
                        target_heat_a = 0.86 * dir_a
                        target_heat_b = 0.86 * dir_b
                        a.heat += (target_heat_a - a.heat) * thermal_k
                        b.heat += (target_heat_b - b.heat) * thermal_k

                        motion = max(0.05, args.speed / 8.0)
                        target_vy_a = -dir_a * 1.45 * motion
                        target_vy_b = -dir_b * 1.45 * motion
                        velocity_k = min(1.0, h * (0.45 + 1.05 * pulse))
                        a.vy += (target_vy_a - a.vy) * velocity_k
                        b.vy += (target_vy_b - b.vy) * velocity_k

                        split_left = max(0.0, split_left - h)
                        # Keep the visible neck for the first half of breakup.
                        touch = max(0.0, 1.0 - phase) * min(1.0, 0.35 + overlap)
                    else:
                        # Capillary attraction and velocity matching create a
                        # stable merged silhouette without replacing either mass.
                        core = surface * 0.30
                        if dist < core:
                            qcore = (core - dist) / max(core, 0.1)
                            f = interaction * (0.10 + 0.35 * qcore)
                            sign = -1.0
                        else:
                            f = interaction * (0.28 + 0.72 * overlap)
                            sign = 1.0
                        stick = min(0.16, h * interaction * (0.7 + 1.8 * overlap))
                        avx = (a.vx * a.mass + b.vx * b.mass) / (a.mass + b.mass)
                        avy = (a.vy * a.mass + b.vy * b.mass) / (a.mass + b.mass)
                        a.vx += (avx - a.vx) * stick
                        a.vy += (avy - a.vy) * stick
                        b.vx += (avx - b.vx) * stick
                        b.vy += (avy - b.vy) * stick
                        touch = min(1.0, 0.20 + overlap * 1.25)

                    a.contact = max(a.contact, touch)
                    b.contact = max(b.contact, touch)
                    a.contact_dx, a.contact_dy = nx, ny
                    b.contact_dx, b.contact_dy = -nx, -ny

                _PAIR_CONTACT[key] = hold
                _PAIR_SPLIT[key] = split_left
                if split_left <= 0.0 and split_dir:
                    _PAIR_SPLIT_DIR.pop(key, None)
                forces[i][0] += nx * f * sign
                forces[i][1] += ny * f * sign
                forces[j][0] -= nx * f * sign
                forces[j][1] -= ny * f * sign

        for i, b in enumerate(blobs):
            fx, fy = forces[i]

            # Real lava-lamp mechanism in miniature: wax heats below the lamp,
            # rises while warm, cools above it, then sinks when denser.
            if args.wall_hit:
                heat_zone = H - 1.0
                cool_zone = 0.0
            else:
                heat_zone = H + b.radius * 0.75
                cool_zone = -b.radius * 0.75

            if b.y > heat_zone:
                b.heat += (1.0 - b.heat) * min(1.0, h * 1.35)
            elif b.y < cool_zone:
                b.heat += (-1.0 - b.heat) * min(1.0, h * 1.20)
            else:
                # Wax keeps its thermal state while crossing the visible
                # chamber. This hysteresis is what carries it all the way to
                # the opposite reservoir instead of stalling halfway.
                b.heat *= max(0.0, 1.0 - h * 0.002)

            # Buoyancy. Positive heat accelerates upward (negative screen Y).
            motion = max(0.05, args.speed / 8.0)
            fy += -b.heat * (1.35 * motion)

            # Horizontal motion is deliberately small but clearly alive. The
            # lane spring prevents clustering at one side, while three very slow
            # incommensurate convection terms create a non-repeating drift.
            fx += (b.anchor_x - b.x) * 0.010 * spread
            conv = (0.62 * math.sin(t * 0.23 + b.phase) +
                    0.31 * math.sin(t * 0.091 + b.phase2) +
                    0.19 * math.sin(t * b.drift_rate + b.drift_phase) +
                    b.drift_bias)
            fx += conv * 0.095 * args.wobble

            # Edge pressure is smooth and acts before the blob clips sideways.
            margin = max(1.5, b.radius * 0.75)
            if b.x < margin:
                fx += (margin - b.x) * 0.28
            elif b.x > W - 1 - margin:
                fx -= (b.x - (W - 1 - margin)) * 0.28

            # Semi-implicit Euler + viscous drag gives convincing inertia.
            b.vx += fx * h
            b.vy += fy * h
            drag_x = math.exp(-0.48 * h)
            drag_y = math.exp(-0.32 * h)
            b.vx *= drag_x
            b.vy *= drag_y
            b.vx = max(-2.2, min(2.2, b.vx))
            b.vy = max(-3.4 * motion, min(3.4 * motion, b.vy))
            b.x += b.vx * h
            b.y += b.vy * h

            if args.wall_hit:
                # Optional visible edge bounce retained from the earlier build.
                top = b.radius * 0.35
                bottom = H - 1 - b.radius * 0.35
                if b.y < top:
                    b.y = top
                    b.vy = abs(b.vy) * 0.72
                    b.heat = min(b.heat, -0.45)
                elif b.y > bottom:
                    b.y = bottom
                    b.vy = -abs(b.vy) * 0.72
                    b.heat = max(b.heat, 0.45)

        # Smooth visual geometry independently from conserved physical area.
        # A critically damped-like exponential response makes merge/split read
        # as surface-tension deformation instead of an instantaneous resize.
        for b in blobs:
            grow_rate = 3.0 if b.visual_radius < b.radius else 2.2
            k = 1.0 - math.exp(-grow_rate * h)
            b.visual_radius += (b.radius - b.visual_radius) * k
            b.visual_strength += (1.0 - b.visual_strength) * (1.0 - math.exp(-3.6 * h))

        # Shape is derived from current velocity and nearest-neighbour pull.
        for i, b in enumerate(blobs):
            nearest = None
            nearest_d = 1e9
            for j, o in enumerate(blobs):
                if i == j:
                    continue
                dx, dy = o.x - b.x, o.y - b.y
                d = math.hypot(dx, dy)
                if d < nearest_d:
                    nearest_d = d
                    nearest = (dx, dy)

            motion = max(0.05, args.speed / 8.0)
            speed_y = min(1.0, abs(b.vy) / max(0.8, 2.4 * motion))
            speed_x = min(1.0, abs(b.vx) / 1.1)
            breathe = 1.0 + args.pulse * math.sin(t * b.shape_rate + b.shape_phase)
            # Contact makes each participating blob bulge toward the other.
            # Because their fields overlap, the visible contour becomes one
            # larger rounded mass with a narrowing neck at first contact.
            cap = min(1.0, b.contact)
            vr = max(0.8, b.visual_radius)
            b.rx = vr * breathe * (1.07 - 0.18 * speed_y + 0.10 * speed_x + 0.22 * cap)
            b.ry = vr * breathe * (0.95 + 0.34 * speed_y - 0.06 * speed_x + 0.16 * cap)

            if nearest is not None:
                dx, dy = nearest
                d = max(0.001, nearest_d)
                reach = b.radius * 3.2
                affinity = max(0.0, 1.0 - d / reach)
                ux, uy = dx / d, dy / d
                # Secondary lobe leans toward a neighbour, producing a neck
                # before the metaball cores actually touch.
                cap = min(1.0, b.contact)
                pull_x = b.contact_dx if cap > 0.02 else ux
                pull_y = b.contact_dy if cap > 0.02 else uy
                b.lobe_dx = (ux * affinity * 0.62 + pull_x * cap * 0.72) * b.radius + b.vx * 0.18
                b.lobe_dy = (uy * affinity * 0.62 + pull_y * cap * 0.72) * b.radius + (0.45 if b.vy > 0 else -0.45) * speed_y
                b.lobe_w = 0.18 + 0.30 * affinity + 0.34 * cap
            else:
                b.lobe_dx = b.vx * 0.18
                b.lobe_dy = (0.45 if b.vy > 0 else -0.45) * speed_y
                b.lobe_w = 0.20

def render(blobs, t, dt, args, colors):
    lava, hi, bg = colors
    out = [bg] * (W * H)
    glow_enabled = not args.no_glow
    softness = max(0.02, min(1.5, args.softness))
    density = max(0.15, args.density)
    brightness = max(0.05, args.brightness)

    states = [b.render_state() for b in blobs]
    for y in range(H):
        for x in range(W):
            field = 0.0
            heat = 0.0
            for st, b in zip(states, blobs):
                bx, by = st["x"], st["y"]
                rx, ry = st["rx"], st["ry"]

                # Elliptical metaball core.  Normalising by rx/ry lets the
                # silhouette stretch without changing its overall mass too much.
                dx = (x - bx) / rx
                dy = (y - by) / ry
                d2 = dx * dx + dy * dy + 0.11
                strength = st.get("strength", 1.0)
                contribution = strength / d2

                # Smaller attached lobe makes the blob shape evolve over time.
                lx = bx + st["lobe_dx"]
                ly = by + st["lobe_dy"]
                ldx = (x - lx) / max(0.8, rx * 0.72)
                ldy = (y - ly) / max(0.8, ry * 0.72)
                lobe = strength * st["lobe_w"] / (ldx * ldx + ldy * ldy + 0.16)

                contribution += lobe
                field += contribution
                heat += contribution * (0.72 + 0.28 * math.sin(b.shape_phase + t * 0.35))

            f = field * 0.54 * density
            edge = 1.0 / (1.0 + math.exp(-(f - 1.0) / (0.10 + softness * 0.26)))
            if glow_enabled:
                halo = max(0.0, min(1.0, (f - 0.28) / 0.72)) * 0.22
            else:
                halo = 0.0

            if edge < 0.015 and halo <= 0.0:
                continue

            molten = min(1.0, edge * 1.08)
            hotness = max(0.0, min(1.0, heat / max(field, 1e-6) - 0.55))
            c = _mix(lava, hi, min(1.0, molten * 0.58 + hotness * 0.55))
            c = _scale(c, brightness * (0.72 + 0.28 * molten))
            if halo > 0.0:
                halo_c = _scale(lava, 0.24 * brightness)
                c = _mix(_mix(bg, halo_c, halo), c, molten)
            else:
                c = _mix(bg, c, molten)
            out[y * W + x] = c

    # subtle lamp glass shading at extreme edges, preserving the black-ish background
    for y in range(H):
        for x in (0, 1, W - 2, W - 1):
            i = y * W + x
            out[i] = _mix(bg, out[i], 0.66 if x in (1, W - 2) else 0.42)
    return out


def main():
    args = parse_args()
    args.fps = max(1.0, min(30.0, args.fps))
    args.blobs = max(1, min(18, args.blobs))
    args.min_radius = max(0.8, min(8.0, args.min_radius))
    args.max_radius = max(args.min_radius, min(10.0, args.max_radius))
    args.speed = max(0.05, min(12.0, args.speed))
    args.wobble = max(0.0, min(4.0, args.wobble))
    args.pulse = max(0.0, min(0.65, args.pulse))
    args.interaction = max(0.0, min(4.0, args.interaction))
    args.spread = max(0.0, min(4.0, args.spread))
    args.split_radius = max(args.min_radius * 1.25, min(16.0, args.split_radius))
    args.split_delay = max(0.2, min(8.0, args.split_delay))

    p_lava, p_hi, p_bg = PALETTES[args.palette]
    try:
        lava = _hex(args.lava or p_lava)
        hi = _hex(args.highlight or p_hi)
        bg = _hex(args.background or p_bg)
    except ValueError as e:
        raise SystemExit(str(e))

    rng = random.Random(args.seed)
    xs = _initial_x_positions(rng, args)
    blobs = [Blob(rng, args, i, xs[i]) for i in range(args.blobs)]
    interval = 1.0 / args.fps

    # Physics runs at a fixed 60 Hz, completely decoupled from rendering and
    # network upload latency.  The previous variable-dt loop fed the sometimes
    # uneven HTTP frame time back into the simulation; merge/split frames are a
    # little more expensive, so that produced visible speed changes / jerks.
    PHYSICS_DT = 1.0 / 60.0
    sim_t = 0.0
    accumulator = 0.0
    clock_prev = time.monotonic()
    next_frame = clock_prev

    print(f"lava-lamp -> {_base(args.host)}  palette={args.palette} blobs={args.blobs} fps={args.fps:g}  (Ctrl-C to stop)")
    try:
        if args.test:
            # Advance a few seconds so --test shows an evolved, interacting frame.
            sim_t = 0.0
            for _ in range(120):
                _advance_physics(blobs, 1.0 / 30.0, sim_t, args)
                sim_t += 1.0 / 30.0
            pixels = render(blobs, sim_t, interval, args, (lava, hi, bg))
            status = show(args.host, pixels)
            print(f"test: drew 1 frame (status {status})")
            return

        while True:
            now = time.monotonic()

            # Accumulate real elapsed time, but never try to catch up more than
            # 100 ms after a temporary network/device stall.  Advancing in
            # constant 1/60 s slices keeps forces, coalescence and fission
            # numerically smooth and prevents a slow frame from creating a big
            # visible jump on the next one.
            wall_dt = min(0.10, max(0.0, now - clock_prev))
            clock_prev = now
            accumulator += wall_dt
            while accumulator >= PHYSICS_DT:
                _advance_physics(blobs, PHYSICS_DT, sim_t, args)
                sim_t += PHYSICS_DT
                accumulator -= PHYSICS_DT

            # Render on an absolute cadence instead of sleep(elapsed).  This
            # avoids accumulating scheduling drift from PNG encoding and HTTP
            # upload time.
            if now < next_frame:
                time.sleep(next_frame - now)
                continue

            pixels = render(blobs, sim_t, PHYSICS_DT, args, (lava, hi, bg))
            status = show(args.host, pixels)
            if status not in (200, 201, 204, 409):
                print(f"draw status {status}")

            next_frame += interval
            after = time.monotonic()
            if next_frame < after - interval:
                # If the device was unavailable for a while, drop missed render
                # slots rather than bursting several frames back-to-back.
                next_frame = after + interval
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        clear(args.host)


if __name__ == "__main__":
    main()

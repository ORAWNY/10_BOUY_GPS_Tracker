#!/usr/bin/env python3
# tri_compass_folder_trails_legend_stacked.py
# Wind + Wave + Current from one folder of TXT logs.
# - 3 instruments with up to 6 arrows (newest→oldest) with alpha fade
# - Numeric degree labels INSIDE the dial (no N/NE letters).
# - Center shows freshest timestamp (LOCAL Europe/Amsterdam with DST).
# - Legend chips INSIDE the dial, stacked: LABEL (colored) + direction (black) over speed (black).
# - Bearing tick marker drawn OUTSIDE the rim.

import math
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from datetime import datetime

import matplotlib
import matplotlib.pyplot as plt
import sys

# Font family (emoji-capable is fine even though we no longer use emojis)
if sys.platform.startswith("win"):
    matplotlib.rcParams["font.family"] = ["Segoe UI", "Segoe UI Emoji", "Segoe UI Symbol", "DejaVu Sans"]
elif sys.platform == "darwin":
    matplotlib.rcParams["font.family"] = ["Helvetica", "Apple Color Emoji", "DejaVu Sans"]
else:
    matplotlib.rcParams["font.family"] = ["DejaVu Sans", "Noto Color Emoji"]

# -------- Timezones (UTC → Europe/Amsterdam using pytz; falls back to zoneinfo) --------
try:
    import pytz
    TZ_UTC = pytz.UTC
    TZ_LOCAL = pytz.timezone("Europe/Amsterdam")
except Exception:
    from datetime import timezone
    try:
        from zoneinfo import ZoneInfo  # py>=3.9
        TZ_UTC = timezone.utc
        TZ_LOCAL = ZoneInfo("Europe/Amsterdam")
    except Exception:
        TZ_UTC = timezone.utc
        TZ_LOCAL = timezone.utc  # fallback

# ===================== USER CONFIG ==========================

FOLDER_PATH = r"D:\04_Met_Ocean\01_Client_Projects\06_Monitoring\data\Compass_testing"
DEBUG = False

# WIND (e.g., LW... with "... AMRWDirT,204.05, AMRSpd,0.28, ...")
WIND_PREFIX    = "LW"
WIND_DIR_COL   = "AMRWDirT"
WIND_SPD_COL   = "AMRSpd"
WIND_UNIT      = "m/s"
WIND_COLOR     = "#22C55E"  # green
WIND_LABEL     = "Wind"

# WAVE
WAVE_PREFIX    = "MW"
WAVE_DIR_COL   = "W6"
WAVE_SPD_COL   = "W8"
WAVE_UNIT      = "m/s"
WAVE_COLOR     = "#A855F7"  # purple
WAVE_LABEL     = "Wave"

# CURRENT
CURRENT_PREFIX = "MA"
CURRENT_DIR_COL = "A3"
CURRENT_SPD_COL = "A2"
CURRENT_UNIT    = "m/s"
CURRENT_COLOR   = "#3B82F6"  # blue
CURRENT_LABEL   = "Current"

# History/staleness
HISTORY_COUNT  = 6
STALE_MINUTES  = 30
STALE_ALPHA    = 0.60

# Display / theme
WINDOW_PX     = 350
OUTPUT_PNG    = "compass.png"
TICK_COLOR    = "#8b9097"   # grid/tick lines
RING_OUTLINE  = "#5b6168"   # outer ring
BACKDROP      = "#eceff3"
DIAL_FACE     = "#d7dbe2"
TEXT_COLOR    = "#000000"   # legend & center text

# Degree numbers (subdued relative to grid)
DEG_LABEL_COLOR = "#6b7178"
DEG_LABEL_ALPHA = 0.85

# Numeric degree labels placement (safely inside rim)
LABEL_R       = 0.875
LABEL_PAD     = 0.05
LABEL_ROUND   = 0.06

# Stacked legend inside the dial
# Stacked legend inside the dial
LEG_XS        = [0.1, 0.43, 0.9]
LEG_Y_DIR     = -0.08   # direction (top line)
LEG_Y_SPD     = -0.13  # speed (bottom line)
LABEL_SHIFT    = 0.028  # icon sits slightly left of direction text


# Arrow style (thin line + big head)
ARROW_LW          = 2.2
ARROW_HEAD_SCALE  = 28

# ===========================================================

@dataclass
class Reading:
    theta_deg: Optional[float]
    speed: Optional[float]
    ts: Optional[str]  # YYMMDDHHMMSS (UTC)

@dataclass
class Instrument:
    name: str
    prefix: str
    dir_col: str
    spd_col: str
    unit: str
    color: str
    label: str
    readings: List[Reading]

_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")

def _coerce_float(x) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        try:
            return float(x)
        except Exception:
            return None
    m = _NUM_RE.search(str(x))
    return float(m.group(0)) if m else None

def _parse_line_to_map(line: str) -> Dict[str, float]:
    tokens = [t.strip() for t in line.strip().split(",") if t.strip()]
    data: Dict[str, float] = {}
    i = 0
    while i < len(tokens) - 1:
        key = tokens[i]
        val = tokens[i + 1]
        if _NUM_RE.fullmatch(val) or _NUM_RE.match(val):
            f = _coerce_float(val)
            if f is not None:
                data[key] = f
            i += 2
        else:
            i += 1
    return data

def _list_latest_paths(folder: str, prefix: str, count: int) -> List[Tuple[str, str]]:
    patt = re.compile(r'^' + re.escape(prefix) + r'(\d{12})', re.IGNORECASE)
    cands: List[Tuple[str, str]] = []
    for name in os.listdir(folder):
        m = patt.match(name)
        if not m:
            continue
        ts = m.group(1)
        cands.append((name, ts))
    cands.sort(key=lambda x: x[1], reverse=True)
    out = [(os.path.join(folder, n), ts) for n, ts in cands[:count]]
    if DEBUG:
        print(f"[SCAN] prefix='{prefix}'  matched={len(cands)}  picked={len(out)}")
    return out

def _read_first_data_line(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                if "," in line and any(ch.isdigit() for ch in line):
                    return line
    except Exception:
        pass
    return None

def _read_values_from_file(path: str, dir_col: str, spd_col: str) -> Tuple[Optional[float], Optional[float]]:
    line = _read_first_data_line(path)
    if line is None:
        return None, None
    data = _parse_line_to_map(line)
    return data.get(dir_col), data.get(spd_col)

def _load_instrument(name: str, prefix: str, dir_col: str, spd_col: str, unit: str, color: str, label: str) -> Instrument:
    paths = _list_latest_paths(FOLDER_PATH, prefix, HISTORY_COUNT)
    readings: List[Reading] = []
    for abs_path, ts in paths:
        d, s = _read_values_from_file(abs_path, dir_col, spd_col)
        readings.append(Reading(theta_deg=d, speed=s, ts=ts))
    return Instrument(name, prefix, dir_col, spd_col, unit, color, label, readings)

# ---------------- time helpers (UTC → Europe/Amsterdam) ----------------

def _ts_to_dt_utc(ts: Optional[str]) -> Optional[datetime]:
    if not ts or len(ts) != 12:
        return None
    y = 2000 + int(ts[0:2])
    mo = int(ts[2:4]); dd = int(ts[4:6])
    hh = int(ts[6:8]); mm = int(ts[8:10]); ss = int(ts[10:12])
    try:
        return datetime(y, mo, dd, hh, mm, ss, tzinfo=TZ_UTC)
    except ValueError:
        return None

def _format_center_timestamp_local(best_ts: Optional[str]) -> Tuple[str, str]:
    """Top: DD Mon YY  |  Bottom: HH:MM (Europe/Amsterdam, DST-aware)."""
    dt_utc = _ts_to_dt_utc(best_ts)
    if not dt_utc:
        return "— — —", "—:—"
    dt_local = dt_utc.astimezone(TZ_LOCAL)
    return dt_local.strftime("%d %b %y"), dt_local.strftime("%H:%M")

# -------------- plotting helpers --------------

def _deg_to_rad_clockwise_from_north(deg: float) -> float:
    return math.radians((deg or 0) % 360.0)

def _cardinal_16(deg: Optional[float]) -> Optional[str]:
    if deg is None: return None
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
            "S","SSW","SW","WSW","W","WNW","NW","NNW"]
    idx = int((deg % 360) / 22.5 + 0.5) % 16
    return dirs[idx]

def _plot_tick(ax, deg: float, r0: float, r1: float, lw: float, color: str,
               alpha: float = 1.0, outside=False):
    theta = _deg_to_rad_clockwise_from_north(deg)
    rr0, rr1 = (1.0, 1.045) if outside else (r0, r1)
    ax.plot([theta, theta], [rr0, rr1], lw=lw, color=color, alpha=alpha,
            solid_capstyle="butt", zorder=8, clip_on=False)

# -------------- main draw --------------

def draw_compass(instruments: List[Instrument], save_path: Optional[str], window_px: int = 350):
    # freshest timestamp across all streams (UTC string)
    freshest: Optional[str] = None
    for ins in instruments:
        if ins.readings and ins.readings[0].ts:
            if (freshest is None) or (ins.readings[0].ts > freshest):
                freshest = ins.readings[0].ts
    freshest_dt = _ts_to_dt_utc(freshest)
    ts_top, ts_bottom = _format_center_timestamp_local(freshest)

    dpi = 100
    inches = window_px / dpi
    fig = plt.figure(figsize=(inches, inches), dpi=dpi, facecolor=BACKDROP)
    ax = fig.add_subplot(111, projection="polar", facecolor=BACKDROP)

    # Raise compass a bit to leave room for legend inside if needed
    fig.subplots_adjust(left=0.06, right=0.94, bottom=0.14, top=0.96)

    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)

    # Dial face (donut)
    ax.bar([0], [0.985], width=2*math.pi, bottom=0, color=DIAL_FACE,
           align='edge', edgecolor=None, linewidth=0)

    majors = list(range(0, 360, 30))
    minors = list(range(0, 360, 10))
    ax.set_thetagrids(majors, labels=[""] * len(majors))
    ax.set_rticks([])
    ax.set_rorigin(-0.5)
    ax.set_ylim(0, 1.0)

    # Grid ticks
    for d in minors:
        r0 = 0.92 if d % 30 else 0.88
        ax.plot([_deg_to_rad_clockwise_from_north(d)]*2, [r0, 0.98],
                lw=1 if d % 30 else 2, color=TICK_COLOR, solid_capstyle="butt", zorder=2)

    # Degree numbers INSIDE with subtle dark-grey color & tiny patch
    for d in majors:
        theta = _deg_to_rad_clockwise_from_north(d)
        ax.text(theta, LABEL_R, str(d),
                color=DEG_LABEL_COLOR, alpha=DEG_LABEL_ALPHA,
                fontsize=9, ha="center", va="center",
                bbox=dict(boxstyle=f"round,pad={LABEL_PAD},rounding_size={LABEL_ROUND}",
                          facecolor=DIAL_FACE, edgecolor="none"),
                zorder=6)

    # Inner cutout and outer outline
    ax.bar([0], [0.24], width=2*math.pi, bottom=0, color=BACKDROP,
           align='edge', edgecolor=None, linewidth=0, zorder=3)
    thetas = [math.radians(t) for t in range(0, 361)]
    ax.plot(thetas, [1.0]*len(thetas), lw=2, color=RING_OUTLINE, zorder=4)

    # Arrow alpha ladder: newest 1.0, then [0.5, 0.4, 0.3, 0.2, 0.0]
    max_hist = min(HISTORY_COUNT, 6)
    base_alphas = [1.0, 0.5, 0.4, 0.3, 0.2, 0.0][:max_hist]

    # Draw each instrument's arrows
    for ins in instruments:
        newest_ts = ins.readings[0].ts if ins.readings else None
        newest_dt = _ts_to_dt_utc(newest_ts)
        stale_factor = 1.0
        if freshest_dt and newest_dt:
            diff_min = (freshest_dt - newest_dt).total_seconds() / 60.0
            if diff_min > STALE_MINUTES:
                stale_factor = STALE_ALPHA
        elif freshest_dt and not newest_dt:
            stale_factor = STALE_ALPHA

        # Bearing tick outside rim for newest
        if ins.readings and ins.readings[0].theta_deg is not None:
            _plot_tick(ax, ins.readings[0].theta_deg, 0.84, 1.0, 3.2, ins.color,
                       alpha=stale_factor, outside=True)

        for idx, r in enumerate(ins.readings[:max_hist]):
            if r.theta_deg is None:
                continue
            alpha = base_alphas[idx] * stale_factor
            if alpha <= 0.0:
                continue
            theta = _deg_to_rad_clockwise_from_north(r.theta_deg)
            ax.annotate("",
                        xy=(theta, 0.86), xytext=(theta, 0.26),
                        arrowprops=dict(
                            arrowstyle="-|>",
                            lw=ARROW_LW,
                            color=ins.color,
                            alpha=alpha,
                            mutation_scale=ARROW_HEAD_SCALE
                        ),
                        annotation_clip=False, zorder=5)

    # Center timestamp (freshest, localized to NL time; DD Mon YY on top)
    ax.text(0.5, 0.54, ts_top, transform=ax.transAxes,
            ha="center", va="center", fontsize=11, color=TEXT_COLOR, fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.25,rounding_size=0.12",
                      facecolor=BACKDROP, edgecolor="none"))
    ax.text(0.5, 0.44, ts_bottom, transform=ax.transAxes,
            ha="center", va="center", fontsize=11, color=TEXT_COLOR,
            bbox=dict(boxstyle="round,pad=0.25,rounding_size=0.12",
                      facecolor=BACKDROP, edgecolor="none"))

    # -------- Stacked legend chips INSIDE the dial --------
    def _cardinal_16_local(deg: Optional[float]) -> Optional[str]:
        if deg is None: return None
        dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]
        idx = int((deg % 360) / 22.5 + 0.5) % 16
        return dirs[idx]

    def _top_text(r: Optional[Reading]) -> str:
        if not r or r.theta_deg is None:
            return "No data"
        return f"{int(round(r.theta_deg))}° {_cardinal_16_local(r.theta_deg) or ''}".rstrip()

    def _bottom_text(r: Optional[Reading], unit: str) -> str:
        if not r or r.speed is None:
            return "—"
        return f"{r.speed:g} {unit}"

    # Order: current, wind, wave
    layout = [
        (Instrument("current", CURRENT_PREFIX, CURRENT_DIR_COL, CURRENT_SPD_COL, CURRENT_UNIT,
                    CURRENT_COLOR, CURRENT_LABEL, []), LEG_XS[0]),
        (Instrument("wind", WIND_PREFIX, WIND_DIR_COL, WIND_SPD_COL, WIND_UNIT,
                    WIND_COLOR, WIND_LABEL, []), LEG_XS[1]),
        (Instrument("wave", WAVE_PREFIX, WAVE_DIR_COL, WAVE_SPD_COL, WAVE_UNIT,
                    WAVE_COLOR, WAVE_LABEL, []), LEG_XS[2]),
    ]
    # Inject latest readings from original list
    name_to_latest = {ins.name: (ins.readings[0] if ins.readings else None) for ins in instruments}

    for ins_stub, x in layout:
        latest = name_to_latest.get(ins_stub.name)
        # colored label (e.g., "Current")
        ax.text(x - LABEL_SHIFT, LEG_Y_DIR, ins_stub.label, transform=ax.transAxes,
                ha="right", va="center", fontsize=10.8, color=ins_stub.color, fontweight="bold")
        # black direction (top) and speed (bottom)
        ax.text(x, LEG_Y_DIR, _top_text(latest), transform=ax.transAxes,
                ha="left", va="center", fontsize=10.5, color=TEXT_COLOR)
        ax.text(x, LEG_Y_SPD, _bottom_text(latest, ins_stub.unit), transform=ax.transAxes,
                ha="left", va="center", fontsize=10.5, color=TEXT_COLOR)

    if save_path:
        fig.savefig(save_path, dpi=100, facecolor=BACKDROP)
    plt.show()
    plt.close(fig)

# -------------- run --------------

def main():
    wind    = _load_instrument("wind",   WIND_PREFIX,    WIND_DIR_COL,   WIND_SPD_COL,   WIND_UNIT,   WIND_COLOR,   WIND_LABEL)
    wave    = _load_instrument("wave",   WAVE_PREFIX,    WAVE_DIR_COL,   WAVE_SPD_COL,   WAVE_UNIT,   WAVE_COLOR,   WAVE_LABEL)
    current = _load_instrument("current",CURRENT_PREFIX, CURRENT_DIR_COL, CURRENT_SPD_COL, CURRENT_UNIT, CURRENT_COLOR, CURRENT_LABEL)

    instruments = [wind, wave, current]
    draw_compass(instruments, save_path=OUTPUT_PNG, window_px=WINDOW_PX)

    # Minimal console summary
    for ins in instruments:
        newest = ins.readings[0] if ins.readings else None
        print(f"[SUMMARY] {ins.name:7s} "
              f"ts={(newest.ts if newest else '—')}  "
              f"dir={(int(round(newest.theta_deg)) if newest and newest.theta_deg is not None else '—')}  "
              f"spd={(f'{newest.speed:g} {ins.unit}' if newest and newest.speed is not None else '—')}")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Dr. Robert W. McGwier, PhD
"""
Visible_Ephemeris.py

Real-time satellite visibility monitor using Skyfield.

Features:
- Uses Celestrak TLEs (cached locally).
- Default: visible-only (satellite sunlit AND observer in night).
- Sorts by elevation (highest first).
- Limits printed/streamed satellites via --maxsat.
- Apogee filter: --max-apogee (km) to restrict orbital regime (default 500 km).
- Name include/exclude masks for constellation/mission filtering.
- Optional UDP JSON snapshot output (--udp, --udp-snapshot).
- Optional Web UI via Server-Sent Events (--web host:port).
- Clean terminal UI, press 'q' then Enter to quit.
"""

import argparse
import datetime as dt
import json
import queue
import select
import socket
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

import numpy as np
from skyfield.api import Loader, wgs84
from skyfield.sgp4lib import EarthSatellite

# Optional dependencies
try:
    import requests
except Exception:
    requests = None

try:
    from flask import Flask, Response, render_template_string, stream_with_context
    HAVE_FLASK = True
except Exception:
    HAVE_FLASK = False

# ---------------------------------------------------------------------------
# Global configuration
# ---------------------------------------------------------------------------

VERSION = "2.5.7-apogee-ssefix"

CACHE_DIR = Path("_skyfield_cache")
CACHE_DIR.mkdir(exist_ok=True)

DEFAULT_TLE_FILE = CACHE_DIR / "active.tle"

CELESTRAK_GROUPS = {
    "active":   "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=tle",
    "amateur":  "https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT=tle",
    "starlink": "https://celestrak.org/NORAD/elements/gp.php?GROUP=starlink&FORMAT=tle",
    "geo":      "https://celestrak.org/NORAD/elements/gp.php?GROUP=geo&FORMAT=tle",
    "gnss":     "https://celestrak.org/NORAD/elements/gp.php?GROUP=gnss&FORMAT=tle",
    "visual":   "https://celestrak.org/NORAD/elements/gp.php?GROUP=visual&FORMAT=tle",
}

TWILIGHT_DEGS = {
    "civil": 6.0,
    "nautical": 12.0,
    "astronomical": 18.0,
}

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def utcnow() -> dt.datetime:
    """Return current UTC time without microseconds."""
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def file_is_stale(path: Path, max_age_hours: float) -> bool:
    """True if file missing or older than max_age_hours."""
    if not path.exists():
        return True
    age = time.time() - path.stat().st_mtime
    return age > max_age_hours * 3600.0


def download_tle(url: str, dest: Path, timeout: int = 20) -> None:
    """Download TLE file from URL into dest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if requests is not None:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        dest.write_text(r.text, encoding="utf-8")
    else:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as f:
            data = f.read().decode("utf-8", errors="replace")
        dest.write_text(data, encoding="utf-8")


def abbreviate_name(name: str) -> str:
    """Strip bracket/paren annotations and compress whitespace."""
    import re
    n = re.sub(r"\[[^\]]*\]", "", name)
    n = re.sub(r"\([^)]*\)", "", n)
    return " ".join(n.split())


def compile_mask_list(exprs: Optional[str]) -> Optional[List[str]]:
    """Comma-separated substrings -> list, or None."""
    if not exprs:
        return None
    parts = [e.strip() for e in exprs.split(",") if e.strip()]
    return parts or None


def name_matches(name: str,
                 include: Optional[List[str]],
                 exclude: Optional[List[str]]) -> bool:
    """Return True if name passes include/exclude filters."""
    lname = name.lower()
    if exclude:
        for pat in exclude:
            if pat.lower() in lname:
                return False
    if include:
        for pat in include:
            if pat.lower() in lname:
                return True
        return False
    return True

# ---------------------------------------------------------------------------
# Web UI template (SSE client)
# ---------------------------------------------------------------------------

WEB_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Visible Ephemeris</title>
<style>
body{
  font-family:system-ui,sans-serif;
  margin:1rem;
  background:#05070a;
  color:#f0f3f6;
}
table{
  border-collapse:collapse;
  width:100%;
  font-size:0.9rem;
}
th,td{
  border-bottom:1px solid #222;
  padding:0.25rem 0.4rem;
  text-align:right;
}
th:first-child,td:first-child{
  text-align:left;
}
tr:nth-child(even){
  background:#0b0f16;
}
small{
  color:#9aa4b2;
}
</style>
</head>
<body>
<h1>Visible Ephemeris <small id="epoch"></small></h1>
<table>
  <thead>
    <tr>
      <th>Name</th>
      <th>Az (deg)</th>
      <th>El (deg)</th>
      <th>Range (km)</th>
    </tr>
  </thead>
  <tbody id="rows"></tbody>
</table>
<script>
const epoch = document.getElementById('epoch');
const tbody = document.getElementById('rows');
const es = new EventSource('/events');

es.onmessage = (m) => {
  try {
    const o = JSON.parse(m.data);
    if (o.type === 'snapshot') {
      epoch.textContent = ' — ' + o.epoch_utc;
      tbody.innerHTML = '';
      (o.rows || []).forEach(r => {
        const tr = document.createElement('tr');
        tr.innerHTML =
          '<td style="text-align:left;">' + r.name + '</td>' +
          '<td>' + r.az.toFixed(1) + '</td>' +
          '<td>' + r.el.toFixed(1) + '</td>' +
          '<td>' + r.range_km.toFixed(1) + '</td>';
        tbody.appendChild(tr);
      });
    }
  } catch (e) {
    console.error('Bad SSE data', e, m.data);
  }
};

es.onerror = (e) => {
  console.error('SSE error', e);
};
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Web server (Flask + SSE)
# ---------------------------------------------------------------------------

def start_web_server(q: "queue.Queue", host: str, port: int) -> None:
    """Start Flask app serving HTML + SSE stream from queue q."""
    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template_string(WEB_TEMPLATE)

    @app.route("/events")
    def events():
        @stream_with_context
        def gen():
            while True:
                msg = q.get()
                yield f"data: {json.dumps(msg)}\n\n"

        return Response(
            gen(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    app.run(host=host, port=port, threaded=True)

# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Visible Ephemeris real-time satellite visibility monitor"
    )

    ap.add_argument("--version", action="version", version=VERSION)

    # Observer
    ap.add_argument("--lat", type=float, required=True,
                    help="observer latitude in degrees")
    ap.add_argument("--lon", type=float, required=True,
                    help="observer longitude in degrees")
    ap.add_argument("--elev", type=float, default=0.0,
                    help="observer elevation in meters")

    # Display / timing
    ap.add_argument("--interval", type=float, default=10.0,
                    help="update interval in seconds")
    ap.add_argument("--min-el", type=float, default=0.0,
                    help="minimum elevation (deg) to display")
    ap.add_argument("--maxsat", type=int, default=40,
                    help="maximum satellites shown per update")

    # Visibility
    ap.add_argument("--visible-only", dest="visible_only",
                    action="store_true", default=True,
                    help="show only visible satellites (default)")
    ap.add_argument("--no-visible-only", dest="visible_only",
                    action="store_false",
                    help="disable visible-only filter")

    # Twilight
    ap.add_argument("--twilight", type=str, default="civil",
                    choices=["civil", "nautical", "astronomical", "custom"],
                    help="twilight model")
    ap.add_argument("--twilight-deg", type=float, default=None,
                    help="custom Sun altitude (deg, negative) if twilight=custom")

    # TLE handling
    ap.add_argument("--group", type=str, default="active",
                    choices=sorted(CELESTRAK_GROUPS.keys()),
                    help="Celestrak TLE group")
    ap.add_argument("--tle-url", type=str, default=None,
                    help="override TLE URL")
    ap.add_argument("--tle-file", type=Path, default=DEFAULT_TLE_FILE,
                    help="local TLE cache file path")
    ap.add_argument("--refresh-hrs", type=float, default=24.0,
                    help="max TLE age in hours before refresh")

    # Name masks
    ap.add_argument("--mask-include", type=str, default=None,
                    help="comma-separated substrings; keep names containing any")
    ap.add_argument("--mask-exclude", type=str, default=None,
                    help="comma-separated substrings; drop names containing any")

    # Apogee filter
    ap.add_argument("--max-apogee", type=float, default=500.0,
                    help="maximum apogee altitude in km")

    # UDP output
    ap.add_argument("--udp", type=str, default=None,
                    help="send JSON snapshots to HOST:PORT via UDP")
    ap.add_argument("--udp-snapshot", action="store_true",
                    help="send snapshot JSON frames via UDP each update")
    ap.add_argument("--udp-snapshot-max", type=int, default=50,
                    help="max rows per UDP snapshot message")

    # Web UI
    ap.add_argument("--web", type=str, default=None,
                    help="serve Web UI at HOST:PORT (requires Flask)")

    return ap

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = build_parser().parse_args()

    # Twilight threshold
    if args.twilight == "custom":
        if args.twilight_deg is None or args.twilight_deg >= 0.0:
            print("[ERROR] --twilight custom requires --twilight-deg < 0")
            sys.exit(2)
        sun_alt_thresh = float(args.twilight_deg)
    else:
        sun_alt_thresh = -TWILIGHT_DEGS[args.twilight]

    maxsat = max(1, int(args.maxsat))
    min_el = float(args.min_el)

    # UDP setup
    udp_sock = None
    udp_addr = None
    if args.udp:
        host, port = args.udp.split(":")
        udp_addr = (host, int(port))
        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # Web SSE setup
    sse_queue = None
    if args.web:
        if not HAVE_FLASK:
            print("[ERROR] Flask not installed; cannot use --web")
            sys.exit(2)
        host, port = args.web.split(":")
        sse_queue = queue.Queue(maxsize=128)
        threading.Thread(
            target=start_web_server,
            args=(sse_queue, host, int(port)),
            daemon=True,
        ).start()
        print(f"[INFO] Web UI at http://{host}:{port}/")

    # Skyfield setup
    load = Loader(str(CACHE_DIR))
    ts = load.timescale()
    eph = load("de421.bsp")
    earth = eph["earth"]
    sun = eph["sun"]
    topos = wgs84.latlon(args.lat, args.lon, elevation_m=args.elev)

    # TLE handling (robust, no double _skyfield_cache)
    tle_url = args.tle_url or CELESTRAK_GROUPS[args.group]
    user_tle = Path(args.tle_file)

    if user_tle.is_absolute():
        tle_path = user_tle
    else:
        tle_path = CACHE_DIR / user_tle.name

    if file_is_stale(tle_path, args.refresh_hrs):
        print("[INFO] Fetching TLEs…")
        try:
            download_tle(tle_url, tle_path)
        except Exception as e:
            print(f"[WARN] TLE download failed: {e}")

    try:
        if tle_path.is_absolute():
            satellites = load.tle_file(str(tle_path))
        else:
            satellites = load.tle_file(tle_path.name)
    except Exception as e:
        print(f"[ERROR] loading TLE file {tle_path}: {e}")
        sys.exit(1)

    satellites = [s for s in satellites if isinstance(s, EarthSatellite)]
    if not satellites:
        print("[ERROR] no satellites from TLE file")
        sys.exit(1)

    # Apogee filter
    earth_radius_km = 6371.0
    kept: List[EarthSatellite] = []
    for sat in satellites:
        try:
            a = float(sat.model.a) * float(sat.model.radiusearthkm)
            e = float(sat.model.ecco)
            apogee_alt = a * (1.0 + e) - earth_radius_km
            if apogee_alt <= args.max_apogee:
                kept.append(sat)
        except Exception:
            continue

    satellites = kept
    if not satellites:
        print(f"[ERROR] no satellites after max-apogee filter ({args.max_apogee} km)")
        sys.exit(1)

    print(f"[INFO] Tracking {len(satellites)} satellites after apogee <= {args.max_apogee} km")

    inc = compile_mask_list(args.mask_include)
    exc = compile_mask_list(args.mask_exclude)
    if inc or exc:
        satellites = [s for s in satellites if name_matches(s.name, inc, exc)]
        print(f"[INFO] After name mask filter: {len(satellites)} satellites remain")

    if not satellites:
        print("[ERROR] no satellites remain after filtering")
        sys.exit(1)

    # Main loop
    try:
        while True:
            loop_start = time.perf_counter()
            t = ts.now()
            now = utcnow()

            # Sun altitude and night flag
            sun_alt = (earth + topos).at(t).observe(sun).apparent().altaz()[0].degrees
            is_night = sun_alt <= sun_alt_thresh

            n = len(satellites)
            alts = np.empty(n)
            azs = np.empty(n)
            rngs = np.empty(n)
            sunlit = np.empty(n, dtype=bool)

            for i, sat in enumerate(satellites):
                diff = sat - topos
                alt, az, dist = diff.at(t).altaz()
                alts[i] = alt.degrees
                azs[i] = (az.degrees + 360.0) % 360.0
                rngs[i] = dist.km
                try:
                    sunlit[i] = sat.at(t).is_sunlit(eph)
                except Exception:
                    sunlit[i] = True

            mask = alts >= min_el
            if args.visible_only:
                mask &= sunlit & is_night

            idx = np.where(mask)[0]
            idx = idx[np.argsort(-alts[idx])]
            idx = idx[:maxsat]

            rows = [
                (abbreviate_name(satellites[i].name), azs[i], alts[i], rngs[i])
                for i in idx
            ]

            # Terminal output
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.flush()

            mode = "VISIBLE" if args.visible_only else "ALL"
            print(f"EPOCH: {now:%Y-%m-%d %H:%M:%S}  SunAlt={sun_alt:.1f} deg  Mode={mode}")
            print(f"{'Name':<32} {'Az(deg)':>8} {'El(deg)':>8} {'Range(km)':>12}")
            print("-" * 64)

            if rows:
                for nm, az, el, rng in rows:
                    print(f"{nm:<32.32} {az:8.1f} {el:8.1f} {rng:12.1f}")
            else:
                print("(no satellites match current filters)")

            print()
            print("Press 'q' then Enter to quit.", flush=True)

            snapshot = {
                "type": "snapshot",
                "epoch_utc": now.isoformat(),
                "rows": [
                    {"name": nm, "az": float(az), "el": float(el), "range_km": float(rng)}
                    for (nm, az, el, rng) in rows
                ],
            }

            # UDP snapshot
            if udp_sock is not None and udp_addr is not None and args.udp_snapshot:
                slim = snapshot.copy()
                slim["rows"] = slim["rows"][: max(1, int(args.udp_snapshot_max))]
                try:
                    udp_sock.sendto(json.dumps(slim).encode("utf-8"), udp_addr)
                except Exception:
                    pass

            # Web SSE snapshot
            if sse_queue is not None:
                try:
                    sse_queue.put_nowait(snapshot)
                except queue.Full:
                    pass

            # 'q' to quit
            try:
                rlist, _, _ = select.select([sys.stdin], [], [], 0)
                if sys.stdin in rlist:
                    line = sys.stdin.readline().strip().lower()
                    if line == "q":
                        print("[INFO] Quit requested by user.")
                        break
            except Exception:
                pass

            elapsed = time.perf_counter() - loop_start
            delay = float(args.interval) - elapsed
            if delay > 0:
                time.sleep(delay)

    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user; exiting.")


if __name__ == "__main__":
    main()

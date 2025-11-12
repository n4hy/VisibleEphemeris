#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Dr. Robert W. McGwier
"""
Visible_Ephemeris.py

Real-time satellite visibility monitor using Skyfield with optional Web UI (SSE) and UDP output.

Major features:
- Default "visible-only": satellite in sunlight AND observer at night (configurable twilight model).
- Sorted by elevation (descending), limited by --maxsat.
- Apogee filter (--max-apogee km) to limit orbital regime (default 500 km).
- Name include/exclude substring masks to quickly filter by constellation/family.
- Robust TLE caching with no double _skyfield_cache path issue.
- Web UI via Server-Sent Events (SSE) streaming a simple HTML table.
- UDP snapshot output for microcontrollers/SDR/FPGA.

Usage example:
    ./Visible_Ephemeris.py --lat 21.3069 --lon -157.8583 --visible-only --web 0.0.0.0:8080 --maxsat 40
"""

# ---------------------- Imports ----------------------
import argparse                # Command-line flags parsing
import datetime as dt          # UTC timestamps and formatting
import json                    # JSON for web/UDP payloads
import queue                   # Thread-safe queue for SSE handoff to Flask
import select                  # Non-blocking stdin polling (q to quit)
import socket                  # UDP socket for snapshot broadcasting
import sys                     # Standard I/O and exit control
import threading               # Running Flask server in background thread
import time                    # Sleep and elapsed timing
from pathlib import Path       # Cross-platform path handling
from typing import List, Optional  # Type hints for clarity

import numpy as np             # Lightweight vectorization for filtering/sorting
from skyfield.api import Loader, wgs84         # Skyfield loader and observer coordinates
from skyfield.sgp4lib import EarthSatellite    # Guard type for TLE parsing

# Optional HTTP client for TLE download (faster than urllib if installed)
try:
    import requests            # Optional, used when present
except Exception:
    requests = None            # Fallback path uses urllib if requests missing

# Optional Flask for Web UI (only needed if --web is used)
try:
    from flask import Flask, Response, render_template_string, stream_with_context
    HAVE_FLASK = True
except Exception:
    HAVE_FLASK = False

# ---------------------- Globals & Constants ----------------------
VERSION = "2.5.7-apogee-ssefix"         # Version string returned by --version

CACHE_DIR = Path("_skyfield_cache")     # Local cache directory for TLEs and ephemerides
CACHE_DIR.mkdir(exist_ok=True)          # Ensure cache exists at import time

DEFAULT_TLE_FILE = CACHE_DIR / "active.tle"  # Default TLE filename we manage under cache

# Celestrak TLE groups (configurable via --group)
CELESTRAK_GROUPS = {
    "active":   "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=tle",
    "amateur":  "https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT=tle",
    "starlink": "https://celestrak.org/NORAD/elements/gp.php?GROUP=starlink&FORMAT=tle",
    "geo":      "https://celestrak.org/NORAD/elements/gp.php?GROUP=geo&FORMAT=tle",
    "gnss":     "https://celestrak.org/NORAD/elements/gp.php?GROUP=gnss&FORMAT=tle",
    "visual":   "https://celestrak.org/NORAD/elements/gp.php?GROUP=visual&FORMAT=tle",
}

# Twilight thresholds (Sun altitude below horizon)
TWILIGHT_DEGS = {
    "civil": 6.0,          # Civil twilight = Sun -6 deg
    "nautical": 12.0,      # Nautical twilight = Sun -12 deg
    "astronomical": 18.0,  # Astronomical twilight = Sun -18 deg
}

# ---------------------- Utilities ----------------------
def utcnow() -> dt.datetime:
    """
    Return the current UTC time with seconds precision (microseconds stripped).
    """
    # Get now in UTC and drop microseconds for cleaner display
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def file_is_stale(path: Path, max_age_hours: float) -> bool:
    """
    Return True if the file is missing or older than max_age_hours.
    Used to decide when to refresh TLEs.
    """
    # If file is absent, we must refresh
    if not path.exists():
        return True
    # Compute age in seconds and compare to threshold
    age = time.time() - path.stat().st_mtime
    return age > max_age_hours * 3600.0


def download_tle(url: str, dest: Path, timeout: int = 20) -> None:
    """
    Download a TLE file from the given URL to 'dest'.
    Prefers 'requests' if available, falls back to urllib otherwise.
    Raises if HTTP fails.
    """
    # Ensure destination directory exists
    dest.parent.mkdir(parents=True, exist_ok=True)
    if requests is not None:
        # Use requests if present for better TLS/redirect handling
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        dest.write_text(r.text, encoding="utf-8")
    else:
        # Fallback to urllib
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as f:
            data = f.read().decode("utf-8", errors="replace")
        dest.write_text(data, encoding="utf-8")


def abbreviate_name(name: str) -> str:
    """
    Compress long satellite names for table display by removing bracketed and parenthetical parts.
    """
    import re
    # Strip substrings like [GLONASS-M] or (USA 300)
    n = re.sub(r"\[[^\]]*\]", "", name)
    n = re.sub(r"\([^)]*\)", "", n)
    # Collapse whitespace to single spaces
    return " ".join(n.split())


def compile_mask_list(exprs: Optional[str]) -> Optional[List[str]]:
    """
    Convert a comma-separated string of substrings to a list; None if empty.
    """
    if not exprs:
        return None
    parts = [e.strip() for e in exprs.split(",") if e.strip()]
    return parts or None


def name_matches(name: str,
                 include: Optional[List[str]],
                 exclude: Optional[List[str]]) -> bool:
    """
    Return True if the satellite name passes include/exclude substring filters.
    - If 'exclude' contains a substring found in name → reject.
    - If 'include' is provided → must match at least one include.
    - If 'include' is None → accept unless excluded.
    """
    lname = name.lower()
    # Apply exclude filters if present
    if exclude:
        for pat in exclude:
            if pat.lower() in lname:
                return False
    # Apply include filters if present (must match one)
    if include:
        for pat in include:
            if pat.lower() in lname:
                return True
        return False
    # No include means allow by default
    return True

# ---------------------- Web UI (HTML Template) ----------------------
WEB_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Visible Ephemeris</title>
<style>
body{font-family:system-ui,sans-serif;margin:1rem;background:#05070a;color:#f0f3f6;}
table{border-collapse:collapse;width:100%;font-size:0.9rem;}
th,td{border-bottom:1px solid #222;padding:0.25rem 0.4rem;text-align:right;}
th:first-child,td:first-child{text-align:left;}
tr:nth-child(even){background:#0b0f16;}
small{color:#9aa4b2;}
</style>
</head>
<body>
<h1>Visible Ephemeris <small id="epoch"></small></h1>
<table>
  <thead>
    <tr>
      <th>Name</th><th>Az (deg)</th><th>El (deg)</th><th>Range (km)</th>
    </tr>
  </thead>
  <tbody id="rows"></tbody>
</table>
<script>
const epoch = document.getElementById('epoch');    // DOM node for epoch text
const tbody = document.getElementById('rows');     // DOM node for table rows
const es = new EventSource('/events');             // Open SSE connection to server

es.onmessage = (m) => {
  try {
    const o = JSON.parse(m.data);                  // Parse JSON from SSE 'data:' field
    if (o.type === 'snapshot') {                   // Only process 'snapshot' frames
      epoch.textContent = ' — ' + o.epoch_utc;     // Update epoch
      tbody.innerHTML = '';                        // Clear old rows
      (o.rows || []).forEach(r => {                // Append new rows
        const tr = document.createElement('tr');
        tr.innerHTML =
          '<td style="text-align:left;">' + r.name + '</td>' +
          '<td>' + r.az.toFixed(1) + '</td>' +
          '<td>' + r.el.toFixed(1) + '</td>' +
          '<td>' + r.range_km.toFixed(1) + '</td>';
        tbody.appendChild(tr);
      });
    }
  } catch (e) { console.error('Bad SSE data', e, m.data); } // Log parse errors
};
es.onerror = (e) => { console.error('SSE error', e); };     // Log network/stream errors
</script>
</body>
</html>
"""

# ---------------------- Web Server (Flask + SSE) ----------------------
def start_web_server(q: "queue.Queue", host: str, port: int) -> None:
    """
    Start a minimal Flask app that:
    - Serves the HTML UI at "/"
    - Streams JSON snapshots via SSE at "/events", pulling from queue q
    """
    app = Flask(__name__)  # Create Flask app instance

    @app.route("/")
    def index():
        # Render simple HTML page defined above
        return render_template_string(WEB_TEMPLATE)

    @app.route("/events")
    def events():
        # Use stream_with_context so generator keeps request context alive
        @stream_with_context
        def gen():
            while True:
                msg = q.get()                               # Block until main thread enqueues a snapshot
                yield f"data: {json.dumps(msg)}\n\n"        # Emit SSE event frame (must end with double newline)

        # Return streaming response with proper SSE MIME type and no buffering
        return Response(
            gen(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache",
                     "X-Accel-Buffering": "no"}
        )

    # Run lightweight dev server on given host/port in a separate thread
    app.run(host=host, port=port, threaded=True)

# ---------------------- Argument Parser ----------------------
def build_parser() -> argparse.ArgumentParser:
    """
    Construct and return the command-line argument parser including:
    - Observer location
    - Output formatting and filters
    - TLE source/cache management
    - UDP and Web UI options
    """
    ap = argparse.ArgumentParser(description="Visible Ephemeris real-time satellite visibility monitor")
    ap.add_argument("--version", action="version", version=VERSION)  # Print version and exit

    # Observer geodetic
    ap.add_argument("--lat", type=float, required=True, help="observer latitude in degrees")
    ap.add_argument("--lon", type=float, required=True, help="observer longitude in degrees")
    ap.add_argument("--elev", type=float, default=0.0, help="observer elevation in meters")

    # Display / cadence
    ap.add_argument("--interval", type=float, default=10.0, help="update interval in seconds")
    ap.add_argument("--min-el", type=float, default=0.0, help="minimum elevation (deg) to display")
    ap.add_argument("--maxsat", type=int, default=40, help="maximum satellites shown per update")

    # Visibility selection
    ap.add_argument("--visible-only", dest="visible_only", action="store_true", default=True,
                    help="show only visible satellites (default)")
    ap.add_argument("--no-visible-only", dest="visible_only", action="store_false",
                    help="disable visible-only filter")

    # Twilight model for "night"
    ap.add_argument("--twilight", type=str, default="civil",
                    choices=["civil", "nautical", "astronomical", "custom"],
                    help="twilight model")
    ap.add_argument("--twilight-deg", type=float, default=None,
                    help="custom Sun altitude (deg, negative) if twilight=custom")

    # TLE management
    ap.add_argument("--group", type=str, default="active",
                    choices=sorted(CELESTRAK_GROUPS.keys()),
                    help="Celestrak TLE group")
    ap.add_argument("--tle-url", type=str, default=None, help="override TLE URL")
    ap.add_argument("--tle-file", type=Path, default=DEFAULT_TLE_FILE,
                    help="local TLE cache file path (stored under cache dir if relative)")
    ap.add_argument("--refresh-hrs", type=float, default=24.0,
                    help="max TLE age in hours before refresh")

    # Name filters
    ap.add_argument("--mask-include", type=str, default=None,
                    help="comma-separated substrings; keep names containing any")
    ap.add_argument("--mask-exclude", type=str, default=None,
                    help="comma-separated substrings; drop names containing any")

    # Orbit apogee filter
    ap.add_argument("--max-apogee", type=float, default=500.0,
                    help="maximum apogee altitude in km")

    # UDP telemetry
    ap.add_argument("--udp", type=str, default=None, help="send JSON snapshots to HOST:PORT via UDP")
    ap.add_argument("--udp-snapshot", action="store_true",
                    help="send snapshot JSON frames via UDP each update")
    ap.add_argument("--udp-snapshot-max", type=int, default=50,
                    help="max rows per UDP snapshot message")

    # Web UI
    ap.add_argument("--web", type=str, default=None,
                    help="serve Web UI at HOST:PORT (requires Flask)")

    return ap

# ---------------------- Main Program ----------------------
def main() -> None:
    """
    Entry point:
    - Parse args and build run-time configuration
    - Initialize Skyfield (timescale, ephemeris, observer)
    - Manage TLE cache and load satellites
    - Filter, sort, and format output
    - Stream to terminal, Web SSE, and UDP on an interval
    """
    args = build_parser().parse_args()                      # Parse CLI flags into 'args'

    # Resolve Sun altitude threshold for "night" based on twilight model
    if args.twilight == "custom":
        if args.twilight_deg is None or args.twilight_deg >= 0.0:
            print("[ERROR] --twilight custom requires --twilight-deg < 0")
            sys.exit(2)
        sun_alt_thresh = float(args.twilight_deg)           # Use provided negative angle
    else:
        sun_alt_thresh = -TWILIGHT_DEGS[args.twilight]      # Convert named model to negative degrees

    maxsat = max(1, int(args.maxsat))                       # Ensure at least one row prints
    min_el = float(args.min_el)                             # Minimum elevation cutoff

    # Initialize UDP objects if requested
    udp_sock = None
    udp_addr = None
    if args.udp:
        host, port = args.udp.split(":")                    # Parse HOST:PORT
        udp_addr = (host, int(port))                        # Build address tuple
        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # Create UDP socket

    # Initialize Web SSE components if requested
    sse_queue = None
    if args.web:
        if not HAVE_FLASK:
            print("[ERROR] Flask not installed; cannot use --web")
            sys.exit(2)
        host, port = args.web.split(":")                    # Parse serve bind
        sse_queue = queue.Queue(maxsize=128)                # Queue for producer (main) → consumer (/events)
        threading.Thread(                                   # Launch the Flask server in background
            target=start_web_server,
            args=(sse_queue, host, int(port)),
            daemon=True,
        ).start()
        print(f"[INFO] Web UI at http://{host}:{port}/")

    # Initialize Skyfield loader and ephemerides
    load = Loader(str(CACHE_DIR))                           # Loader anchored to cache directory
    ts = load.timescale()                                   # Timescale (UTC)
    eph = load("de421.bsp")                                 # Planetary ephemeris file (auto-cached)
    earth = eph["earth"]                                    # Reference to Earth body
    sun = eph["sun"]                                        # Reference to Sun body
    topos = wgs84.latlon(args.lat, args.lon, elevation_m=args.elev)  # Observer geodetic position

    # ---------------- TLE Handling (robust to avoid double cache path) ----------------
    tle_url = args.tle_url or CELESTRAK_GROUPS[args.group]  # Resolve source URL based on --group unless overridden
    user_tle = Path(args.tle_file)                          # User-provided path (absolute or relative)

    if user_tle.is_absolute():
        tle_path = user_tle                                 # Absolute path → use as-is
    else:
        tle_path = CACHE_DIR / user_tle.name                # Relative → normalize under cache dir

    if file_is_stale(tle_path, args.refresh_hrs):           # Refresh TLEs when missing or older than threshold
        print("[INFO] Fetching TLEs…")
        try:
            download_tle(tle_url, tle_path)                 # Download and write the TLE file
        except Exception as e:
            print(f"[WARN] TLE download failed: {e}")       # Non-fatal: loader may still have cached copy

    try:
        if tle_path.is_absolute():
            satellites = load.tle_file(str(tle_path))       # Absolute path can be passed directly
        else:
            satellites = load.tle_file(tle_path.name)       # For relative, pass ONLY the filename (Loader adds cache dir)
    except Exception as e:
        print(f"[ERROR] loading TLE file {tle_path}: {e}")  # Hard error → exit
        sys.exit(1)
    # ----------------------------------------------------------------------------------

    # Keep only valid EarthSatellite objects (filter out comments/headers)
    satellites = [s for s in satellites if isinstance(s, EarthSatellite)]
    if not satellites:
        print("[ERROR] no satellites from TLE file")
        sys.exit(1)

    # Apogee filter in km (reject if apogee altitude > --max-apogee)
    earth_radius_km = 6371.0                                # Mean Earth radius for altitude conversion
    kept: List[EarthSatellite] = []
    for sat in satellites:
        try:
            a = float(sat.model.a) * float(sat.model.radiusearthkm)  # Semi-major axis in km
            e = float(sat.model.ecco)                                # Eccentricity
            apogee_alt = a * (1.0 + e) - earth_radius_km             # Altitude at apogee above Earth's surface
            if apogee_alt <= args.max_apogee:
                kept.append(sat)                                     # Keep if within limit
        except Exception:
            continue                                                 # Skip malformed entries

    satellites = kept
    if not satellites:
        print(f"[ERROR] no satellites after max-apogee filter ({args.max_apogee} km)")
        sys.exit(1)

    print(f"[INFO] Tracking {len(satellites)} satellites after apogee <= {args.max_apogee} km")

    # Apply include/exclude name masks if provided
    inc = compile_mask_list(args.mask_include)
    exc = compile_mask_list(args.mask_exclude)
    if inc or exc:
        satellites = [s for s in satellites if name_matches(s.name, inc, exc)]
        print(f"[INFO] After name mask filter: {len(satellites)} satellites remain")
    if not satellites:
        print("[ERROR] no satellites remain after filtering")
        sys.exit(1)

    # ---------------- Real-time loop ----------------
    try:
        while True:
            loop_start = time.perf_counter()                 # Start elapsed timing for interval control
            t = ts.now()                                     # Current Skyfield time
            now = utcnow()                                   # Human-readable UTC timestamp for display and JSON

            # Determine Sun altitude at observer and derive night/day boolean
            sun_alt = (earth + topos).at(t).observe(sun).apparent().altaz()[0].degrees
            is_night = sun_alt <= sun_alt_thresh

            # Pre-allocate arrays for performance
            n = len(satellites)
            alts = np.empty(n); azs = np.empty(n); rngs = np.empty(n)
            sunlit = np.empty(n, dtype=bool)

            # Compute per-satellite topocentric alt/az/range and sunlight state
            for i, sat in enumerate(satellites):
                diff = sat - topos                           # Vector from observer to satellite
                alt, az, dist = diff.at(t).altaz()           # Compute apparent alt/az/distance
                alts[i] = alt.degrees
                azs[i] = (az.degrees + 360.0) % 360.0        # Normalize azimuth to [0, 360)
                rngs[i] = dist.km
                try:
                    sunlit[i] = sat.at(t).is_sunlit(eph)     # Uses planetary ephemeris to decide sunlight
                except Exception:
                    sunlit[i] = True                         # Conservative: assume sunlit if check fails

            # Build visibility mask using elevation threshold
            mask = alts >= min_el
            if args.visible_only:
                mask &= sunlit & is_night                    # Visible = sunlit AND observer in night

            # Select indices that pass filters, sort by descending elevation, cap at --maxsat
            idx = np.where(mask)[0]
            idx = idx[np.argsort(-alts[idx])]
            idx = idx[:maxsat]

            # Build display rows (name, az, el, range)
            rows = [
                (abbreviate_name(satellites[i].name), azs[i], alts[i], rngs[i])
                for i in idx
            ]

            # -------- Terminal output (clear screen + header + table) --------
            sys.stdout.write("\x1b[2J\x1b[H"); sys.stdout.flush()  # ANSI clear + home
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

            # -------- Snapshot object for UDP and Web SSE --------
            snapshot = {
                "type": "snapshot",
                "epoch_utc": now.isoformat(),
                "rows": [
                    {"name": nm, "az": float(az), "el": float(el), "range_km": float(rng)}
                    for (nm, az, el, rng) in rows
                ],
            }

            # Optionally send compact snapshot via UDP each interval
            if udp_sock is not None and udp_addr is not None and args.udp_snapshot:
                slim = snapshot.copy()
                slim["rows"] = slim["rows"][: max(1, int(args.udp_snapshot_max))]
                try:
                    udp_sock.sendto(json.dumps(slim).encode("utf-8"), udp_addr)
                except Exception:
                    pass  # UDP failures are non-fatal

            # Optionally enqueue snapshot for Web UI to stream via SSE
            if sse_queue is not None:
                try:
                    sse_queue.put_nowait(snapshot)           # Non-blocking; drop frame if queue is full
                except queue.Full:
                    pass

            # Check for quit request (readable stdin, 'q' + Enter)
            try:
                rlist, _, _ = select.select([sys.stdin], [], [], 0)
                if sys.stdin in rlist:
                    line = sys.stdin.readline().strip().lower()
                    if line == "q":
                        print("[INFO] Quit requested by user.")
                        break
            except Exception:
                pass

            # Honor --interval by subtracting compute time
            elapsed = time.perf_counter() - loop_start
            delay = float(args.interval) - elapsed
            if delay > 0:
                time.sleep(delay)

    except KeyboardInterrupt:
        # Handle Ctrl+C gracefully
        print("\n[INFO] Interrupted by user; exiting.")

# ---------------------- Entrypoint ----------------------
if __name__ == "__main__":
    # Standard Python module guard to allow import without executing main()
    main()

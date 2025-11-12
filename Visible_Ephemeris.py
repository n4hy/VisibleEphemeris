#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Dr. Robert W. McGwier, PhD
"""
Visible_Ephemeris.py - IMPROVED VERSION

Real-time satellite visibility monitor using Skyfield.
This version implements high-priority improvements from the code analysis.

Key Improvements:
- Better error handling and input validation
- Resource cleanup with context managers
- Pre-allocated numpy arrays for performance
- Improved robustness in orbital calculations
- Better logging infrastructure
"""

import argparse
import datetime as dt
import json
import logging
import queue
import re
import select
import socket
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

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
# Constants
# ---------------------------------------------------------------------------

VERSION = "2.5.8-improved"

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

# Physical constants
EARTH_RADIUS_KM = 6371.0  # Mean Earth radius in kilometers
FULL_CIRCLE_DEG = 360.0    # Degrees in a circle

# Regex patterns (pre-compiled for efficiency)
_BRACKET_PATTERN = re.compile(r"\[[^\]]*\]")
_PAREN_PATTERN = re.compile(r"\([^)]*\)")

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SatelliteObservation:
    """Single satellite observation data."""
    name: str
    azimuth_deg: float
    elevation_deg: float
    range_km: float
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "az": self.azimuth_deg,
            "el": self.elevation_deg,
            "range_km": self.range_km
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
    n = _BRACKET_PATTERN.sub("", name)
    n = _PAREN_PATTERN.sub("", n)
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


def parse_hostport(hostport_str: str, context: str) -> Tuple[str, int]:
    """
    Parse 'host:port' string with validation.
    
    Args:
        hostport_str: String in format "host:port"
        context: Context string for error messages (e.g., "--udp")
    
    Returns:
        Tuple of (host, port)
    
    Raises:
        ValueError: If format is invalid or port is out of range
    """
    parts = hostport_str.split(":")
    if len(parts) != 2:
        raise ValueError(f"{context}: expected HOST:PORT, got '{hostport_str}'")
    
    host, port_str = parts
    
    try:
        port = int(port_str)
        if not (1 <= port <= 65535):
            raise ValueError(f"{context}: port must be 1-65535, got {port}")
    except ValueError as e:
        raise ValueError(f"{context}: invalid port '{port_str}'") from e
    
    return host, port


def validate_orbital_elements(sat: EarthSatellite, max_apogee_km: float) -> bool:
    """
    Validate satellite orbital elements and check apogee.
    
    Args:
        sat: EarthSatellite object
        max_apogee_km: Maximum allowed apogee altitude in km
    
    Returns:
        True if satellite passes validation and apogee check
    """
    try:
        # Check that required attributes exist
        if not hasattr(sat.model, 'a') or not hasattr(sat.model, 'ecco'):
            logger.debug(f"Satellite {sat.name} missing orbital elements")
            return False
        
        # Extract semi-major axis and eccentricity
        a = float(sat.model.a) * float(sat.model.radiusearthkm)
        e = float(sat.model.ecco)
        
        # Sanity checks for orbital elements
        # e should be in [0, 1) for elliptical orbits (TLEs are always elliptical)
        if e < 0.0 or e >= 1.0:
            logger.debug(f"Satellite {sat.name} has invalid eccentricity: {e}")
            return False
        
        if a <= 0.0:
            logger.debug(f"Satellite {sat.name} has invalid semi-major axis: {a}")
            return False
        
        # Calculate apogee altitude above Earth surface
        # apogee_radius = a(1 + e), apogee_altitude = apogee_radius - R_earth
        apogee_alt = a * (1.0 + e) - EARTH_RADIUS_KM
        
        return apogee_alt <= max_apogee_km
        
    except (AttributeError, ValueError, TypeError) as ex:
        logger.debug(f"Error validating {sat.name}: {ex}")
        return False


@contextmanager
def udp_socket_context(hostport: Optional[str]):
    """
    Context manager for UDP socket with automatic cleanup.
    
    Args:
        hostport: Optional "host:port" string
    
    Yields:
        Tuple of (socket, address) or (None, None)
    """
    if hostport is None:
        yield None, None
        return
    
    sock = None
    try:
        host, port = parse_hostport(hostport, "--udp")
        addr = (host, port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        yield sock, addr
    except ValueError as e:
        logger.error(str(e))
        yield None, None
    finally:
        if sock is not None:
            sock.close()

# ---------------------------------------------------------------------------
# Web UI template (SSE client) - unchanged from original
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
# Web server (Flask + SSE) - unchanged from original
# ---------------------------------------------------------------------------

def start_web_server(q: queue.Queue, host: str, port: int) -> None:
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
# CLI parser - same as original with logging option added
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build argument parser."""
    ap = argparse.ArgumentParser(
        description="Real-time satellite visibility monitor (Improved Version)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    # Observer location
    ap.add_argument("--lat", type=float, required=True,
                    help="observer latitude (degrees)")
    ap.add_argument("--lon", type=float, required=True,
                    help="observer longitude (degrees)")
    ap.add_argument("--elev", type=float, default=0.0,
                    help="observer elevation (meters)")

    # Display options
    ap.add_argument("--interval", type=float, default=1.0,
                    help="refresh interval (seconds)")
    ap.add_argument("--maxsat", type=int, default=20,
                    help="max satellites to display/stream")
    ap.add_argument("--min-el", type=float, default=0.0,
                    help="minimum elevation (degrees)")
    ap.add_argument("--visible-only", action="store_true",
                    help="only show sunlit satellites in dark sky")
    ap.add_argument("--twilight", type=str, default="astronomical",
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
    
    # Logging
    ap.add_argument("--debug", action="store_true",
                    help="enable debug logging")

    return ap

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = build_parser().parse_args()
    
    # Set logging level
    if args.debug:
        logger.setLevel(logging.DEBUG)
    
    # Twilight threshold
    if args.twilight == "custom":
        if args.twilight_deg is None or args.twilight_deg >= 0.0:
            logger.error("--twilight custom requires --twilight-deg < 0")
            sys.exit(2)
        sun_alt_thresh = float(args.twilight_deg)
    else:
        sun_alt_thresh = -TWILIGHT_DEGS[args.twilight]

    maxsat = max(1, int(args.maxsat))
    min_el = float(args.min_el)

    # Web SSE setup
    sse_queue = None
    if args.web:
        if not HAVE_FLASK:
            logger.error("Flask not installed; cannot use --web")
            sys.exit(2)
        try:
            host, port = parse_hostport(args.web, "--web")
            sse_queue = queue.Queue(maxsize=128)
            threading.Thread(
                target=start_web_server,
                args=(sse_queue, host, port),
                daemon=True,
            ).start()
            logger.info(f"Web UI at http://{host}:{port}/")
        except ValueError as e:
            logger.error(str(e))
            sys.exit(2)

    # Skyfield setup
    load = Loader(str(CACHE_DIR))
    ts = load.timescale()
    eph = load("de421.bsp")
    earth = eph["earth"]
    sun = eph["sun"]
    topos = wgs84.latlon(args.lat, args.lon, elevation_m=args.elev)

    # TLE handling (simplified logic)
    tle_url = args.tle_url or CELESTRAK_GROUPS[args.group]
    user_tle = Path(args.tle_file)

    # BOB's new version



    # Determine absolute TLE path
    if user_tle.is_absolute():
        tle_path = user_tle
    elif str(user_tle).startswith(str(CACHE_DIR)):
        # Already has cache dir prefix (e.g., from DEFAULT_TLE_FILE)
        tle_path = user_tle
    else:
        # Relative paths are relative to CACHE_DIR
        tle_path = CACHE_DIR / user_tle

    
    # Ensure parent directory exists
    tle_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Download if stale
    if file_is_stale(tle_path, args.refresh_hrs):
        logger.info("Fetching TLEs…")
        try:
            download_tle(tle_url, tle_path)
        except Exception as e:
            logger.warning(f"TLE download failed: {e}")
            if not tle_path.exists():
                logger.error(f"No TLE file available at {tle_path}")
                sys.exit(1)

    # Load TLEs
    try:
        satellites = load.tle_file(str(tle_path))
    except Exception as e:
        logger.error(f"loading TLE file {tle_path}: {e}")
        sys.exit(1)

    satellites = [s for s in satellites if isinstance(s, EarthSatellite)]
    if not satellites:
        logger.error("no satellites from TLE file")
        sys.exit(1)

    # Apply apogee filter with improved validation
    logger.info(f"Filtering satellites with apogee <= {args.max_apogee} km")
    satellites = [s for s in satellites if validate_orbital_elements(s, args.max_apogee)]
    
    if not satellites:
        logger.error(f"no satellites after max-apogee filter ({args.max_apogee} km)")
        sys.exit(1)

    logger.info(f"Tracking {len(satellites)} satellites after apogee filter")

    # Apply name masks
    inc = compile_mask_list(args.mask_include)
    exc = compile_mask_list(args.mask_exclude)
    if inc or exc:
        satellites = [s for s in satellites if name_matches(s.name, inc, exc)]
        logger.info(f"After name mask filter: {len(satellites)} satellites remain")

    if not satellites:
        logger.error("no satellites remain after filtering")
        sys.exit(1)

    # Pre-allocate numpy arrays for performance
    n = len(satellites)
    alts = np.empty(n)
    azs = np.empty(n)
    rngs = np.empty(n)
    sunlit = np.empty(n, dtype=bool)

    # Main loop with UDP context manager for automatic cleanup
    with udp_socket_context(args.udp) as (udp_sock, udp_addr):
        if udp_sock is None and args.udp:
            # parse_hostport failed, error already logged
            sys.exit(2)
        
        try:
            while True:
                loop_start = time.perf_counter()
                t = ts.now()
                now = utcnow()

                # Sun altitude and night flag
                sun_alt = (earth + topos).at(t).observe(sun).apparent().altaz()[0].degrees
                is_night = sun_alt <= sun_alt_thresh

                # Compute ephemeris for all satellites with error handling
                for i, sat in enumerate(satellites):
                    try:
                        diff = sat - topos
                        alt, az, dist = diff.at(t).altaz()
                        alts[i] = alt.degrees
                        # Skyfield azimuth is already in [0, 360), but normalize for safety
                        azs[i] = az.degrees % FULL_CIRCLE_DEG
                        rngs[i] = dist.km
                        
                        try:
                            sunlit[i] = sat.at(t).is_sunlit(eph)
                        except Exception:
                            # Conservative: assume sunlit if check fails
                            sunlit[i] = True
                            
                    except Exception as ex:
                        # Mark satellite as invalid (below horizon)
                        alts[i] = -90.0
                        azs[i] = 0.0
                        rngs[i] = 0.0
                        sunlit[i] = False
                        logger.debug(f"Computation failed for {sat.name}: {ex}")

                # Apply elevation mask (satellites above minimum elevation angle)
                mask = alts >= min_el
                
                # For visible-only mode, require:
                #   1. Satellite is sunlit (solar panels illuminated)
                #   2. Observer is in darkness (sun below twilight threshold)
                # This ensures the satellite is visible to the naked eye
                if args.visible_only:
                    mask &= sunlit & is_night

                # Sort by elevation (highest first) and limit to maxsat
                idx = np.where(mask)[0]
                idx = idx[np.argsort(-alts[idx])]
                idx = idx[:maxsat]

                # Build observation list
                observations = [
                    SatelliteObservation(
                        name=abbreviate_name(satellites[i].name),
                        azimuth_deg=azs[i],
                        elevation_deg=alts[i],
                        range_km=rngs[i]
                    )
                    for i in idx
                ]

                # Terminal output
                sys.stdout.write("\x1b[2J\x1b[H")
                sys.stdout.flush()

                mode = "VISIBLE" if args.visible_only else "ALL"
                print(f"EPOCH: {now:%Y-%m-%d %H:%M:%S}  SunAlt={sun_alt:.1f} deg  Mode={mode}")
                print(f"{'Name':<32} {'Az(deg)':>8} {'El(deg)':>8} {'Range(km)':>12}")
                print("-" * 64)

                if observations:
                    for obs in observations:
                        print(f"{obs.name:<32.32} {obs.azimuth_deg:8.1f} "
                              f"{obs.elevation_deg:8.1f} {obs.range_km:12.1f}")
                else:
                    print("(no satellites match current filters)")

                print()
                print("Press 'q' then Enter to quit.", flush=True)

                # Build JSON snapshot
                snapshot = {
                    "type": "snapshot",
                    "epoch_utc": now.isoformat(),
                    "rows": [obs.to_dict() for obs in observations],
                }

                # UDP snapshot
                if udp_sock is not None and udp_addr is not None and args.udp_snapshot:
                    slim = snapshot.copy()
                    slim["rows"] = slim["rows"][: max(1, int(args.udp_snapshot_max))]
                    try:
                        udp_sock.sendto(json.dumps(slim).encode("utf-8"), udp_addr)
                    except Exception as ex:
                        logger.debug(f"UDP send failed: {ex}")

                # Web SSE snapshot
                if sse_queue is not None:
                    try:
                        sse_queue.put_nowait(snapshot)
                    except queue.Full:
                        pass

                # Check for 'q' to quit
                try:
                    rlist, _, _ = select.select([sys.stdin], [], [], 0)
                    if sys.stdin in rlist:
                        line = sys.stdin.readline().strip().lower()
                        if line == "q":
                            logger.info("Quit requested by user.")
                            break
                except Exception:
                    pass

                # Performance metrics
                elapsed = time.perf_counter() - loop_start
                if args.debug:
                    logger.debug(f"Computation: {elapsed:.3f}s, "
                               f"Visible: {len(idx)}/{n} satellites")
                
                delay = float(args.interval) - elapsed
                if delay > 0:
                    time.sleep(delay)

        except KeyboardInterrupt:
            logger.info("Interrupted by user; exiting.")


if __name__ == "__main__":
    main()

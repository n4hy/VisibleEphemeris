#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Dr. Robert W. McGwier, PhD
"""
Visible_Ephemeris_Phase1.py

Real-time satellite visibility monitor with Phase 1 enhancements:
- Fisheye all-sky projection
- Enhanced web UI with color-coded satellites
- Click-to-view details panel
- Modern dark theme styling
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
from dataclasses import dataclass, asdict
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
    from flask import Flask, Response, render_template_string
    HAVE_FLASK = True
except Exception:
    HAVE_FLASK = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION = "3.0.0-phase1"

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

EARTH_RADIUS_KM = 6371.0
FULL_CIRCLE_DEG = 360.0

# Regex patterns (pre-compiled)
_BRACKET_PATTERN = re.compile(r"\[[^\]]*\]")
_PAREN_PATTERN = re.compile(r"\([^)]*\)")

# Special satellites for highlighting
SPECIAL_SATELLITES = {
    "ISS", "HUBBLE", "TIANGONG", "CSS", "HST"
}

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
    """Enhanced satellite observation data with visibility info."""
    name: str
    azimuth_deg: float
    elevation_deg: float
    range_km: float
    sunlit: bool
    is_special: bool = False
    norad_id: int = 0
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        return asdict(self)
    
    def get_color_code(self) -> str:
        """
        Return color code based on visibility status.
        - Red: Special satellites (ISS, Hubble, etc.)
        - Green: Sunlit and visible
        - Gray: In shadow
        """
        if self.is_special:
            return "red"
        elif self.sunlit:
            return "green"
        else:
            return "gray"

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


def is_special_satellite(name: str) -> bool:
    """Check if satellite is in special list."""
    name_upper = name.upper()
    return any(special in name_upper for special in SPECIAL_SATELLITES)


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
    """Parse 'host:port' string with validation."""
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
    """Validate satellite orbital elements and check apogee."""
    try:
        if not hasattr(sat.model, 'a') or not hasattr(sat.model, 'ecco'):
            return False
        
        a = float(sat.model.a) * float(sat.model.radiusearthkm)
        e = float(sat.model.ecco)
        
        if e < 0.0 or e >= 1.0 or a <= 0.0:
            return False
        
        apogee_alt = a * (1.0 + e) - EARTH_RADIUS_KM
        return apogee_alt <= max_apogee_km
        
    except (AttributeError, ValueError, TypeError):
        return False


@contextmanager
def udp_socket_context(hostport: Optional[str]):
    """Context manager for UDP socket with automatic cleanup."""
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
# Enhanced Web UI template with fisheye skymap
# ---------------------------------------------------------------------------

WEB_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Visible Ephemeris - Phase 1</title>
<style>
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

body {
  font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
  background: #0a0e14;
  color: #e6edf3;
  padding: 1rem;
  min-height: 100vh;
}

.container {
  max-width: 1600px;
  margin: 0 auto;
}

header {
  margin-bottom: 1.5rem;
  border-bottom: 2px solid #1f2937;
  padding-bottom: 1rem;
}

h1 {
  font-size: 1.75rem;
  font-weight: 600;
  color: #58a6ff;
  margin-bottom: 0.5rem;
}

.epoch {
  color: #8b949e;
  font-size: 0.9rem;
}

.sun-info {
  display: inline-block;
  margin-left: 1rem;
  padding: 0.25rem 0.75rem;
  background: #161b22;
  border-radius: 6px;
  font-size: 0.85rem;
}

.main-content {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1.5rem;
  margin-bottom: 1.5rem;
}

@media (max-width: 1024px) {
  .main-content {
    grid-template-columns: 1fr;
  }
}

.panel {
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 8px;
  padding: 1.5rem;
  box-shadow: 0 4px 6px rgba(0,0,0,0.3);
}

.panel-title {
  font-size: 1.1rem;
  font-weight: 600;
  margin-bottom: 1rem;
  color: #58a6ff;
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

/* Skymap Canvas */
.skymap-container {
  position: relative;
  width: 100%;
  max-width: 600px;
  margin: 0 auto;
}

#skymap {
  width: 100%;
  height: auto;
  display: block;
  cursor: crosshair;
  background: #0d1117;
  border-radius: 8px;
}

.skymap-legend {
  display: flex;
  justify-content: center;
  gap: 1.5rem;
  margin-top: 1rem;
  font-size: 0.85rem;
  flex-wrap: wrap;
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.legend-dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  border: 2px solid currentColor;
}

.legend-dot.special { background: #ff4444; border-color: #ff4444; }
.legend-dot.visible { background: #44ff44; border-color: #44ff44; }
.legend-dot.eclipsed { background: #666666; border-color: #666666; }

/* Satellite Table */
.sat-table-container {
  overflow-x: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.9rem;
}

thead {
  background: #0d1117;
  position: sticky;
  top: 0;
  z-index: 10;
}

th {
  text-align: right;
  padding: 0.75rem 0.5rem;
  font-weight: 600;
  color: #8b949e;
  border-bottom: 2px solid #30363d;
}

th:first-child {
  text-align: left;
}

td {
  padding: 0.5rem;
  text-align: right;
  border-bottom: 1px solid #21262d;
}

td:first-child {
  text-align: left;
}

tbody tr {
  transition: background-color 0.2s;
  cursor: pointer;
}

tbody tr:hover {
  background: #1c2128;
}

tbody tr.selected {
  background: #1f2937;
  border-left: 3px solid #58a6ff;
}

.sat-name {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.sat-indicator {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.sat-indicator.red { background: #ff4444; }
.sat-indicator.green { background: #44ff44; }
.sat-indicator.gray { background: #666666; }

/* Details Panel */
.details-panel {
  background: #1c2128;
  padding: 1rem;
  border-radius: 6px;
  margin-top: 1rem;
  display: none;
}

.details-panel.visible {
  display: block;
}

.details-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 1rem;
  margin-top: 0.75rem;
}

.detail-item {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.detail-label {
  font-size: 0.75rem;
  color: #8b949e;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.detail-value {
  font-size: 1.1rem;
  font-weight: 600;
  color: #e6edf3;
}

.close-details {
  float: right;
  background: none;
  border: none;
  color: #8b949e;
  cursor: pointer;
  font-size: 1.5rem;
  line-height: 1;
  padding: 0;
  width: 24px;
  height: 24px;
}

.close-details:hover {
  color: #e6edf3;
}

/* Status messages */
.no-satellites {
  text-align: center;
  padding: 2rem;
  color: #8b949e;
  font-style: italic;
}

/* Loading indicator */
.loading {
  text-align: center;
  padding: 2rem;
  color: #8b949e;
}

.loading::after {
  content: '...';
  animation: dots 1.5s infinite;
}

@keyframes dots {
  0%, 20% { content: '.'; }
  40% { content: '..'; }
  60%, 100% { content: '...'; }
}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>🛰️ Visible Ephemeris</h1>
    <div>
      <span class="epoch" id="epoch">Connecting...</span>
      <span class="sun-info" id="sunInfo"></span>
    </div>
  </header>

  <div class="main-content">
    <!-- Skymap Panel -->
    <div class="panel">
      <div class="panel-title">
        <span>🌌</span>
        <span>All-Sky View</span>
      </div>
      <div class="skymap-container">
        <canvas id="skymap" width="600" height="600"></canvas>
      </div>
      <div class="skymap-legend">
        <div class="legend-item">
          <div class="legend-dot special"></div>
          <span>Special (ISS, Hubble)</span>
        </div>
        <div class="legend-item">
          <div class="legend-dot visible"></div>
          <span>Sunlit & Visible</span>
        </div>
        <div class="legend-item">
          <div class="legend-dot eclipsed"></div>
          <span>Eclipsed</span>
        </div>
      </div>
    </div>

    <!-- Satellite List Panel -->
    <div class="panel">
      <div class="panel-title">
        <span>📡</span>
        <span>Visible Satellites</span>
        <span style="margin-left: auto; font-size: 0.9rem; font-weight: normal;" id="satCount">0</span>
      </div>
      <div class="sat-table-container">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Az (°)</th>
              <th>El (°)</th>
              <th>Range (km)</th>
            </tr>
          </thead>
          <tbody id="satTableBody">
            <tr><td colspan="4" class="loading">Waiting for data</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Details Panel -->
  <div class="panel" id="detailsPanel">
    <div class="panel-title">
      <span>ℹ️</span>
      <span id="detailsTitle">Satellite Details</span>
      <button class="close-details" onclick="closeDetails()">×</button>
    </div>
    <div class="details-grid" id="detailsGrid"></div>
  </div>
</div>

<script>
// State
let currentData = null;
let selectedSatellite = null;

// Canvas setup
const canvas = document.getElementById('skymap');
const ctx = canvas.getContext('2d');
const centerX = canvas.width / 2;
const centerY = canvas.height / 2;
const radius = Math.min(centerX, centerY) - 40;

// SSE Connection
const es = new EventSource('/events');

es.onmessage = (m) => {
  try {
    const data = JSON.parse(m.data);
    
    // Validate data structure
    if (!data || data.type !== 'snapshot') {
      console.log('Ignoring non-snapshot data');
      return;
    }
    
    if (!data.rows || !Array.isArray(data.rows)) {
      console.error('Data missing rows array:', data);
      return;
    }
    
    // Validate each satellite has required fields
    const validRows = data.rows.filter(sat => {
      if (!sat.az || !sat.el || !sat.range_km) {
        console.warn('Skipping invalid satellite:', sat);
        return false;
      }
      return true;
    });
    
    console.log('Rendering', validRows.length, 'valid satellites');
    data.rows = validRows;
    
    currentData = data;
    updateUI(data);
    drawSkymap(data);
    
  } catch (e) {
    console.error('Parse error:', e.message);
    console.error('Data was:', m.data.substring(0, 200));
  }
};

es.onerror = (e) => {
  console.error('SSE error', e);
  document.getElementById('epoch').textContent = 'Connection error';
};

// Update UI elements
function updateUI(data) {
  console.log('updateUI called with', data.rows?.length, 'rows');
  // Update epoch and sun info
  document.getElementById('epoch').textContent = data.epoch_utc;
  document.getElementById('sunInfo').textContent = 
    `Sun: ${data.sun_alt?.toFixed(1) || 0}° | ${data.is_night ? '🌙 Night' : '☀️ Day'}`;
  
  // Update satellite count
  document.getElementById('satCount').textContent = 
    `${data.rows.length} satellite${data.rows.length !== 1 ? 's' : ''}`;
  
  // Update table
  updateTable(data.rows);
}

// Update satellite table
function updateTable(rows) {
  const tbody = document.getElementById('satTableBody');
  
  if (!rows || rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="no-satellites">No satellites visible</td></tr>';
    return;
  }
  
  tbody.innerHTML = rows.map((sat, idx) => {
    // Safety checks for all values
    const name = sat.name || 'Unknown';
    const color = sat.color || 'gray';
    const az = (sat.az !== undefined) ? sat.az.toFixed(1) : '0.0';
    const el = (sat.el !== undefined) ? sat.el.toFixed(1) : '0.0';
    const range = (sat.range_km !== undefined) ? sat.range_km.toFixed(1) : '0.0';
    
    return `
    <tr onclick="selectSatellite(${idx})" id="sat-row-${idx}">
      <td>
        <div class="sat-name">
          <div class="sat-indicator ${color}"></div>
          <span>${name}</span>
        </div>
      </td>
      <td>${az}</td>
      <td>${el}</td>
      <td>${range}</td>
    </tr>
    `;
  }).join('');
}

// Draw fisheye skymap
function drawSkymap(data) {
  console.log('drawSkymap called, satellites:', data.rows?.length);
  // Clear canvas
  ctx.fillStyle = '#0d1117';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  
  // Draw elevation circles
  ctx.strokeStyle = '#30363d';
  ctx.lineWidth = 1;
  [30, 60, 90].forEach(el => {
    const r = ((90 - el) / 90) * radius;
    ctx.beginPath();
    ctx.arc(centerX, centerY, r, 0, 2 * Math.PI);
    ctx.stroke();
  });
  
  // Draw cardinal directions
  ctx.fillStyle = '#8b949e';
  ctx.font = '14px sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  
  const labelRadius = radius + 25;
  ctx.fillText('N', centerX, centerY - labelRadius);
  ctx.fillText('E', centerX + labelRadius, centerY);
  ctx.fillText('S', centerX, centerY + labelRadius);
  ctx.fillText('W', centerX - labelRadius, centerY);
  
  // Draw horizon circle
  ctx.strokeStyle = '#58a6ff';
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(centerX, centerY, radius, 0, 2 * Math.PI);
  ctx.stroke();
  
  // Draw satellites
  if (data.rows && data.rows.length > 0) {
    data.rows.forEach((sat, idx) => {
      // Skip if required data is missing
      if (sat.az === undefined || sat.el === undefined) {
        console.warn('Skipping satellite with undefined az/el:', sat.name);
        return;
      }
      
      const pos = azElToXY(sat.az, sat.el);
      
      // Determine color and size
      let color, size;
      if (sat.color === 'red') {
        color = '#ff4444';
        size = 8;
      } else if (sat.color === 'green') {
        color = '#44ff44';
        size = 6;
      } else {
        color = '#666666';
        size = 5;
      }
      
      // Highlight if selected
      if (selectedSatellite === idx) {
        ctx.strokeStyle = '#58a6ff';
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(pos.x, pos.y, size + 4, 0, 2 * Math.PI);
        ctx.stroke();
      }
      
      // Draw satellite
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(pos.x, pos.y, size, 0, 2 * Math.PI);
      ctx.fill();
      
      // Draw label for special satellites
      if (sat.color === 'red') {
        ctx.fillStyle = '#ff4444';
        ctx.font = 'bold 11px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(sat.name, pos.x, pos.y - size - 8);
      }
    });
  }
}

// Convert Az/El to canvas X/Y (fisheye projection)
function azElToXY(az, el) {
  // Radius from center (0 at zenith, radius at horizon)
  const r = ((90 - el) / 90) * radius;
  
  // Angle (0° = North, clockwise)
  // Canvas: 0° = East, so rotate by -90°
  const theta = (az - 90) * Math.PI / 180;
  
  return {
    x: centerX + r * Math.cos(theta),
    y: centerY + r * Math.sin(theta)
  };
}

// Canvas click handler
canvas.addEventListener('click', (e) => {
  if (!currentData || !currentData.rows) return;
  
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  const clickX = (e.clientX - rect.left) * scaleX;
  const clickY = (e.clientY - rect.top) * scaleY;
  
  // Find nearest satellite
  let minDist = Infinity;
  let nearestIdx = -1;
  
  currentData.rows.forEach((sat, idx) => {
    const pos = azElToXY(sat.az, sat.el);
    const dist = Math.sqrt((pos.x - clickX) ** 2 + (pos.y - clickY) ** 2);
    if (dist < minDist && dist < 20) {  // 20px click tolerance
      minDist = dist;
      nearestIdx = idx;
    }
  });
  
  if (nearestIdx >= 0) {
    selectSatellite(nearestIdx);
  }
});

// Select satellite
function selectSatellite(idx) {
  selectedSatellite = idx;
  
  // Update table selection
  document.querySelectorAll('#satTableBody tr').forEach(row => {
    row.classList.remove('selected');
  });
  const row = document.getElementById(`sat-row-${idx}`);
  if (row) {
    row.classList.add('selected');
    row.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  
  // Show details
  showDetails(currentData.rows[idx]);
  
  // Redraw skymap with highlight
  drawSkymap(currentData);
}

// Show satellite details
function showDetails(sat) {
  const panel = document.getElementById('detailsPanel');
  const title = document.getElementById('detailsTitle');
  const grid = document.getElementById('detailsGrid');
  
  title.textContent = sat.name;
  
  grid.innerHTML = `
    <div class="detail-item">
      <div class="detail-label">Azimuth</div>
      <div class="detail-value">${sat.az.toFixed(2)}°</div>
    </div>
    <div class="detail-item">
      <div class="detail-label">Elevation</div>
      <div class="detail-value">${sat.el.toFixed(2)}°</div>
    </div>
    <div class="detail-item">
      <div class="detail-label">Range</div>
      <div class="detail-value">${sat.range_km.toFixed(1)} km</div>
    </div>
    <div class="detail-item">
      <div class="detail-label">Status</div>
      <div class="detail-value">${sat.sunlit ? '☀️ Sunlit' : '🌑 Eclipsed'}</div>
    </div>
    ${sat.is_special ? `
    <div class="detail-item">
      <div class="detail-label">Type</div>
      <div class="detail-value">⭐ Special</div>
    </div>
    ` : ''}
    ${sat.norad_id ? `
    <div class="detail-item">
      <div class="detail-label">NORAD ID</div>
      <div class="detail-value">${sat.norad_id}</div>
    </div>
    ` : ''}
  `;
  
  panel.classList.add('visible');
}

// Close details panel
function closeDetails() {
  document.getElementById('detailsPanel').classList.remove('visible');
  selectedSatellite = null;
  drawSkymap(currentData);
}

// Make canvas responsive
window.addEventListener('resize', () => {
  if (currentData) {
    drawSkymap(currentData);
  }
});
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Web server (Flask + SSE)
# ---------------------------------------------------------------------------

def start_web_server(q: queue.Queue, host: str, port: int) -> None:
    """Start Flask app serving HTML + SSE stream from queue q."""
    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template_string(WEB_TEMPLATE)

    @app.route("/events")
    def events():
        def gen():
            while True:
                msg = q.get()  # Blocks until data available
                yield f"data: {json.dumps(msg)}\n\n"
        
        return Response(
            gen(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )

    app.run(host=host, port=port, threaded=True, debug=False)

# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build argument parser."""
    ap = argparse.ArgumentParser(
        description="Real-time satellite visibility monitor - Phase 1 Enhanced",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    ap.add_argument("--lat", type=float, required=True,
                    help="observer latitude (degrees)")
    ap.add_argument("--lon", type=float, required=True,
                    help="observer longitude (degrees)")
    ap.add_argument("--elev", type=float, default=0.0,
                    help="observer elevation (meters)")

    ap.add_argument("--interval", type=float, default=1.0,
                    help="refresh interval (seconds)")
    ap.add_argument("--maxsat", type=int, default=40,
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

    ap.add_argument("--group", type=str, default="active",
                    choices=sorted(CELESTRAK_GROUPS.keys()),
                    help="Celestrak TLE group")
    ap.add_argument("--tle-url", type=str, default=None,
                    help="override TLE URL")
    ap.add_argument("--tle-file", type=str, default="active.tle",
                    help="TLE filename in cache directory")
    ap.add_argument("--refresh-hrs", type=float, default=24.0,
                    help="max TLE age in hours before refresh")

    ap.add_argument("--mask-include", type=str, default=None,
                    help="comma-separated substrings; keep names containing any")
    ap.add_argument("--mask-exclude", type=str, default=None,
                    help="comma-separated substrings; drop names containing any")

    ap.add_argument("--max-apogee", type=float, default=800.0,
                    help="maximum apogee altitude in km")

    ap.add_argument("--udp", type=str, default=None,
                    help="send JSON snapshots to HOST:PORT via UDP")
    ap.add_argument("--udp-snapshot", action="store_true",
                    help="send snapshot JSON frames via UDP each update")
    ap.add_argument("--udp-snapshot-max", type=int, default=50,
                    help="max rows per UDP snapshot message")

    ap.add_argument("--web", type=str, default=None,
                    help="serve Web UI at HOST:PORT (requires Flask)")
    
    ap.add_argument("--debug", action="store_true",
                    help="enable debug logging")

    return ap

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = build_parser().parse_args()
    
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
            
            # Put initial heartbeat message
            sse_queue.put({
                "type": "heartbeat",
                "message": "Connected"
            })
            
            threading.Thread(
                target=start_web_server,
                args=(sse_queue, host, port),
                daemon=True,
            ).start()
            time.sleep(0.5)  # Give Flask time to start
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

    # TLE handling - use simple filename only
    tle_url = args.tle_url or CELESTRAK_GROUPS[args.group]
    tle_path = CACHE_DIR / args.tle_file
    
    tle_path.parent.mkdir(parents=True, exist_ok=True)
    
    if file_is_stale(tle_path, args.refresh_hrs):
        logger.info("Fetching TLEs…")
        try:
            download_tle(tle_url, tle_path)
        except Exception as e:
            logger.warning(f"TLE download failed: {e}")
            if not tle_path.exists():
                logger.error(f"No TLE file available at {tle_path}")
                sys.exit(1)

    try:
        satellites = load.tle_file(str(tle_path))
    except Exception as e:
        logger.error(f"loading TLE file {tle_path}: {e}")
        sys.exit(1)

    satellites = [s for s in satellites if isinstance(s, EarthSatellite)]
    if not satellites:
        logger.error("no satellites from TLE file")
        sys.exit(1)

    # Apply filters
    logger.info(f"Filtering satellites with apogee <= {args.max_apogee} km")
    satellites = [s for s in satellites if validate_orbital_elements(s, args.max_apogee)]
    
    if not satellites:
        logger.error(f"no satellites after max-apogee filter ({args.max_apogee} km)")
        sys.exit(1)

    logger.info(f"Tracking {len(satellites)} satellites after apogee filter")

    inc = compile_mask_list(args.mask_include)
    exc = compile_mask_list(args.mask_exclude)
    if inc or exc:
        satellites = [s for s in satellites if name_matches(s.name, inc, exc)]
        logger.info(f"After name mask filter: {len(satellites)} satellites remain")

    if not satellites:
        logger.error("no satellites remain after filtering")
        sys.exit(1)

    # Pre-allocate arrays
    n = len(satellites)
    alts = np.empty(n)
    azs = np.empty(n)
    rngs = np.empty(n)
    sunlit = np.empty(n, dtype=bool)

    # Main loop
    with udp_socket_context(args.udp) as (udp_sock, udp_addr):
        if udp_sock is None and args.udp:
            sys.exit(2)
        
        try:
            while True:
                loop_start = time.perf_counter()
                t = ts.now()
                now = utcnow()

                # Sun altitude and night flag
                sun_alt = (earth + topos).at(t).observe(sun).apparent().altaz()[0].degrees
                is_night = sun_alt <= sun_alt_thresh

                # Compute ephemeris
                for i, sat in enumerate(satellites):
                    try:
                        diff = sat - topos
                        alt, az, dist = diff.at(t).altaz()
                        alts[i] = alt.degrees
                        azs[i] = az.degrees % FULL_CIRCLE_DEG
                        rngs[i] = dist.km
                        
                        try:
                            sunlit[i] = sat.at(t).is_sunlit(eph)
                        except Exception:
                            sunlit[i] = True
                            
                    except Exception as ex:
                        alts[i] = -90.0
                        azs[i] = 0.0
                        rngs[i] = 0.0
                        sunlit[i] = False
                        logger.debug(f"Computation failed for {sat.name}: {ex}")

                # Apply filters
                mask = alts >= min_el
                if args.visible_only:
                    mask &= sunlit & is_night

                idx = np.where(mask)[0]
                idx = idx[np.argsort(-alts[idx])]
                idx = idx[:maxsat]

                # Build observations with enhanced data
                # Convert numpy types to native Python types explicitly
                observations = []
                for i in idx:
                    obs = SatelliteObservation(
                        name=abbreviate_name(satellites[i].name),
                        azimuth_deg=float(azs[i]),
                        elevation_deg=float(alts[i]),
                        range_km=float(rngs[i]),
                        sunlit=bool(sunlit[i]),
                        is_special=is_special_satellite(satellites[i].name),
                        norad_id=int(satellites[i].model.satnum)
                    )
                    observations.append(obs)

                # Terminal output
                sys.stdout.write("\x1b[2J\x1b[H")
                sys.stdout.flush()

                mode = "VISIBLE" if args.visible_only else "ALL"
                print(f"EPOCH: {now:%Y-%m-%d %H:%M:%S}  SunAlt={sun_alt:.1f}°  Mode={mode}  "
                      f"Night={'YES' if is_night else 'NO'}")
                print(f"{'Name':<32} {'Az(°)':>8} {'El(°)':>8} {'Range(km)':>12} {'Status':>10}")
                print("-" * 75)

                if observations:
                    for obs in observations:
                        status = "🔴SPEC" if obs.is_special else ("🟢VIS" if obs.sunlit else "⚫ECL")
                        print(f"{obs.name:<32.32} {obs.azimuth_deg:8.1f} "
                              f"{obs.elevation_deg:8.1f} {obs.range_km:12.1f} {status:>10}")
                else:
                    print("(no satellites match current filters)")

                print()
                print("Press 'q' then Enter to quit.", flush=True)

                # Build enhanced JSON snapshot with explicit type conversion
                try:
                    snapshot = {
                        "type": "snapshot",
                        "epoch_utc": now.isoformat(),
                        "sun_alt": float(sun_alt),
                        "is_night": bool(is_night),
                        "rows": []
                    }
                    
                    # Build rows with explicit type conversion
                    for obs in observations:
                        row = {
                            "name": str(obs.name),
                            "az": float(obs.azimuth_deg),
                            "el": float(obs.elevation_deg),
                            "range_km": float(obs.range_km),
                            "sunlit": bool(obs.sunlit),
                            "is_special": bool(obs.is_special),
                            "norad_id": int(obs.norad_id),
                            "color": str(obs.get_color_code())
                        }
                        snapshot["rows"].append(row)
                    
                    # Test JSON serialization
                    _ = json.dumps(snapshot)
                    
                except Exception as ex:
                    logger.error(f"Failed to build snapshot: {ex}")
                    continue

                # Check for quit
                try:
                    rlist, _, _ = select.select([sys.stdin], [], [], 0)
                    if sys.stdin in rlist:
                        line = sys.stdin.readline().strip().lower()
                        if line == "q":
                            logger.info("Quit requested by user.")
                            break
                except Exception:
                    pass

                elapsed = time.perf_counter() - loop_start
                if args.debug:
                    logger.debug(f"Loop: {elapsed:.3f}s, Visible: {len(idx)}/{n}")
                
                # ONLY send data after loop is completely done
                # UDP snapshot
                if udp_sock is not None and udp_addr is not None and args.udp_snapshot:
                    slim = snapshot.copy()
                    slim["rows"] = slim["rows"][: max(1, int(args.udp_snapshot_max))]
                    try:
                        udp_sock.sendto(json.dumps(slim).encode("utf-8"), udp_addr)
                    except Exception as ex:
                        logger.debug(f"UDP send failed: {ex}")

                # Web SSE snapshot - send ONLY after complete computation
                if sse_queue is not None:
                    try:
                        # Clear old items if queue is full
                        while sse_queue.qsize() > 100:
                            try:
                                sse_queue.get_nowait()
                            except queue.Empty:
                                break
                        
                        # Put snapshot in queue ONLY when loop is complete
                        sse_queue.put_nowait(snapshot)
                        
                        if args.debug:
                            logger.debug(f"SSE: Sent complete snapshot with {len(snapshot['rows'])} satellites")
                            
                    except queue.Full:
                        # Queue full, clear it and try again
                        try:
                            sse_queue.get_nowait()
                            sse_queue.put_nowait(snapshot)
                        except Exception as ex:
                            if args.debug:
                                logger.debug(f"SSE queue error: {ex}")
                    except Exception as ex:
                        logger.error(f"SSE queue exception: {ex}")
                
                # NOW wait for next cycle
                delay = float(args.interval) - elapsed
                if delay > 0:
                    time.sleep(delay)

        except KeyboardInterrupt:
            logger.info("Interrupted by user; exiting.")


if __name__ == "__main__":
    main()

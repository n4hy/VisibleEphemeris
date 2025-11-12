# Visible Ephemeris 
**Author:** Dr. Robert W. McGwier, PhD  
**License:** MIT License  

This program provides a *real-time, continuously updating* list of visible satellites above a specified observer location.  
It uses the [Skyfield](https://rhodesmill.org/skyfield/) library for orbital propagation and supports both command-line and web interfaces.

---

## ✨ Highlights

- Real-time console and optional web dashboard.
- TLE auto-refresh (default every 24 h).
- Live filtering by:
  - Elevation angle
  - Name mask include/exclude
  - Visibility (sunlit vs. dark sky)
  - **NEW:** maximum apogee (`--max-apogee`) filter.
- UDP JSON snapshot streaming to microcontrollers, SDRs, or FPGAs.
- Flask-based web UI with live updates.
- “q + Enter” graceful exit in terminal.

---

## 🧠 Core Concepts

| Concept | Description |
|----------|--------------|
| **Observer** | The ground location (lat/lon/elev) where visibility is calculated. |
| **Ephemeris** | Instantaneous position of a satellite relative to the observer. |
| **Visibility** | Determined by sunlight geometry and local solar depression angle. |
| **TLE Cache** | Stored in `_skyfield_cache/active.tle` and auto-updated. |
| **UDP Snapshot** | JSON structure broadcast to the specified IP:port. |
| **Web Server** | Live HTML/JS interface for browser-based visualization. |

---

## 🚀 Quick Start

```bash
pip install skyfield numpy flask requests
python Visible_Ephemeris_2_5_7_apogee.py \
    --lat 39.55 --lon -76.13 \
    --web 0.0.0.0:8080 \
    --udp 127.0.0.1:6001 \
    --visible-only

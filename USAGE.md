# Command-Line Usage — Visible Ephemeris 2.5.7-apogee

### General Syntax
python Visible_Ephemeris_2_5_7_apogee.py [options]
python Visible_Ephemeris_2_5_7_apogee.py [options]

---

## 🌍 Core Options

| Option | Description | Default |
|--------|--------------|----------|
| `--lat`, `--lon` | Observer latitude & longitude (degrees) | *required* |
| `--elev` | Elevation above sea level (meters) | 0.0 |
| `--interval` | Time between updates (seconds) | 10 |
| `--min-el` | Minimum elevation (degrees) | 0 |
| `--maxsat` | Maximum satellites shown | 40 |
| `--visible-only` | Display visible satellites (satellite sunlit, observer dark) | Enabled |
| `--no-visible-only` | Disable visibility filter | — |
| `--max-apogee` | **NEW:** Maximum apogee altitude (km) for satellites to include | 500 |
| `--group` | Celestrak group (e.g., active, starlink, weather) | active |
| `--tle-url` | Custom TLE source URL | Auto |
| `--refresh-hrs` | Hours before re-fetching TLE | 24 |
| `--mask-include` | Comma-separated substrings to include | — |
| `--mask-exclude` | Comma-separated substrings to exclude | — |

---

## 🌐 Networking and Web Interface

| Option | Description | Example |
|--------|--------------|----------|
| `--web host:port` | Start Flask web dashboard | `--web 0.0.0.0:8080` |
| `--udp ip:port` | Send JSON snapshot packets via UDP | `--udp 127.0.0.1:6001` |
| `--udp-snapshot` | Send one JSON snapshot per update cycle | — |
| `--udp-snapshot-max` | Maximum satellites per UDP packet | 50 |

Access the web interface at:  
http://<host>:<port>/

---

## 🌙 Twilight Configuration

| Option | Description | Default |
|--------|--------------|----------|
| `--twilight [civil|nautical|astronomical|custom]` | Defines night threshold | civil |
| `--twilight-deg <angle>` | Custom solar depression angle (<0) | — |

---

## 🛰 Output Example

EPOCH: 2025-11-09 03:59:16 SunAlt=-38.1° Mode=VISIBLE
Name Az(deg) El(deg) Range(km)

GSAT0217 47.1 76.8 23368.0
BEIDOU-3 M5 209.0 76.8 21667.6
...
Press 'q' then Enter to quit.


---

## 🔧 Example Command

```bash
python Visible_Ephemeris_2_5_7_apogee.py \
  --lat 34.05 --lon -118.25 \
  --max-apogee 600 \
  --visible-only \
  --web 0.0.0.0:8080

⌨️ Quit Key

Press q then Enter at any time to gracefully stop the program.

📦 Data Files
File	Purpose
_skyfield_cache/active.tle	Cached TLE data (auto-updated)
de421.bsp	Planetary ephemeris file (downloaded automatically)


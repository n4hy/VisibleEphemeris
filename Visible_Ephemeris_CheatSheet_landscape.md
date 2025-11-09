# Visible Ephemeris Cheat Sheet (Landscape Format)
### Version 2.5.7-apogee — Dr. Robert W. McGwier, PhD

---

## ⚙️ Runtime Flags

| Flag | Meaning | Default | Notes |
|------|----------|----------|-------|
| `--lat`, `--lon` | Observer latitude & longitude | *required* | Decimal degrees |
| `--elev` | Elevation above sea level (m) | 0.0 | |
| `--interval` | Update delay (s) | 10 | Display refresh period |
| `--min-el` | Minimum elevation (°) | 0 | Filter horizon clutter |
| `--maxsat` | Satellites shown per screen | 40 | Re-headers every page |
| `--max-apogee` | **NEW:** Max apogee (km) | 500 | Excludes high-orbit sats |
| `--visible-only` | Show visible (sunlit sats only) | Enabled | Disable via `--no-visible-only` |
| `--twilight` | Night definition | civil | Options: civil, nautical, astronomical, custom |
| `--twilight-deg` | Solar depression (°) | derived | Required if `custom` |

---

## 🌐 Networking & Web

| Flag | Description | Example |
|------|--------------|----------|
| `--udp ip:port` | Send live JSON snapshot packets | `--udp 127.0.0.1:6001` |
| `--udp-snapshot` | One JSON per refresh | — |
| `--udp-snapshot-max` | Limit satellites per UDP packet | 50 |
| `--web host:port` | Flask web dashboard | `--web 0.0.0.0:8080` |

Access the dashboard:

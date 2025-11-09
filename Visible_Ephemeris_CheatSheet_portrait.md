# Visible Ephemeris Quick Reference (Portrait)

**Version:** 2.5.7-apogee  
**Author:** Dr. Robert W. McGwier, PhD  

---

### Basic Run
```bash
python Visible_Ephemeris_2_5_7_apogee.py \
  --lat <deg> --lon <deg> --visible-only

Key Controls
Flag	Description
--interval	Seconds between updates
--maxsat	Max satellites displayed
--max-apogee	Max apogee altitude (km)
--group	Celestrak group
--refresh-hrs	Re-fetch period for TLE
--udp, --udp-snapshot	JSON UDP output
--web	Launch browser dashboard
q	Quit
Visibility Logic

Visible = Satellite in sunlight and observer below twilight threshold
(--twilight or --twilight-deg)

Output Columns
Column	Unit	Meaning
Az(deg)	°	Azimuth
El(deg)	°	Elevation
Range(km)	km	Slant range
Example

Key Controls
Flag	Description
--interval	Seconds between updates
--maxsat	Max satellites displayed
--max-apogee	Max apogee altitude (km)
--group	Celestrak group
--refresh-hrs	Re-fetch period for TLE
--udp, --udp-snapshot	JSON UDP output
--web	Launch browser dashboard
q	Quit
Visibility Logic

Visible = Satellite in sunlight and observer below twilight threshold
(--twilight or --twilight-deg)

Output Columns
Column	Unit	Meaning
Az(deg)	°	Azimuth
El(deg)	°	Elevation
Range(km)	km	Slant range
Example

python Visible_Ephemeris_2_5_7_apogee.py \
  --lat 39.55 --lon -76.13 \
  --max-apogee 600 \
  --web 0.0.0.0:8080


---

### 📜 `LICENSE`
```text
MIT License

Copyright (c) 2025 Dr. Robert W. McGwier, PhD

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.


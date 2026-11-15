# Burning Activity Calendar: website + data contract

`index.html` is the whole frontend: one file, no build step. Open it in a browser, or drop it on Vercel / GitHub Pages.
It currently runs on **mock data** (badge in the top-right says so). Mock numbers are fake and only show the shape of the real data.

## Real NASA FIRMS data (uses the MAP_KEY)
The map dots and the hotspot CSV can show **real** FIRMS detections (MODIS + VIIRS S-NPP). The calendar and charts stay on mock numbers until the harmonized dataset is plugged in.

**On your computer**
1. Keep `.env` next to `server.py`. It holds `FIRMS_MAP_KEY=...` and, optionally, `OPENWEATHER_KEY=...` (adds live weather and a 5-day outlook to the map popups; display only, the forecast model doesn't use it).
2. In this folder run `python server.py`, then open **http://localhost:8000** (not the file directly).
3. The badge should say **LIVE FIRMS DOTS · MOCK CALENDAR**. The default range is the last 30 days. Longer ranges are capped at 366 days and take longer, because FIRMS only allows 5 days per request.
4. Big example: pick the preset **Australia: Black Summer (Sep 2019–Feb 2020)**. That is about 1.4 million detections and 74 FIRMS requests, so the first load takes roughly 30 seconds to a few minutes depending on your internet. Archive answers are then saved (gzipped) in `.cache/` (about 40 MB for that example), and reloading takes about 5 seconds. You can delete `.cache/` any time. It is git-ignored and never served.

**Real calendar + Wildfire outlook**
The calendar, charts, KPIs and harmonization box use real NASA FIRMS data from `data/monthly_1deg.json.gz` (monthly MODIS counts per 1° square for the whole world, plus VIIRS for California, Australia, Brazil and Africa, 2012 to last month). Rebuild it after new downloads with `python ml/monthly.py`. `server.py` serves it at `/api/monthly?bbox=W,S,E,N` (harmonized with a per-month VIIRS/MODIS ratio fitted on 2012-2018 and tested on 2019 onwards).
The **Wildfire outlook** card (`/api/outlook?lat=..&lon=..`) rates the next 30 days for any place and lists the reasons: fire history in that square (how many years burned at this time of year, fire season, trend, last detection), the ML forecast, the long-term climate for the month (NASA POWER, no key) and this week's weather vs normal (OpenWeather). The weather adjustment is a stated rule (about 15% per factor), not part of the trained model. In the USA the verdict is about **reported wildfires** (NIFC WFIGS incident records, 2015 on, public, no key): how often a wildfire over 100 acres started within 50 km at this time of year, the wildfire season, and who/what starts fires there (lightning vs. people: arson, debris burning, equipment, campfires, power lines...). Satellite detections, which also include planned and farm burns, are shown as supporting evidence. Outside the USA there's no open incident record, so the verdict uses satellite fire detections and says so. These two routes exist only in `server.py`, not in the Vercel functions.

**3D view (map button under + / −)**
Tilts the map over whatever spot you're looking at. Zoomed out: terrain (AWS Terrain Tiles) with the fire heat map. Zoomed in to a city (zoom 12+): 3D buildings. Optional **Google Photorealistic 3D Tiles**: enable the *Map Tiles API* in Google Cloud, create an API key restricted to that API, and add `GOOGLE_MAPS_KEY=...` to `.env`. The browser loads tiles from `server.py` at `/v1/3dtiles/...`, which adds the key (it never reaches the browser) and stores nothing. Each visitor can start at most 30 new 3D sessions an hour, to protect billing. If the tiles don't load, the page falls back to the standard 3D view.

**Fire Connect page (`/connect.html`)**
A live message board for forecasters, first responders and the public. Anyone can post a fire/smoke report (with a map pin) or a question. Forecasters and responders who enter `RESPONDER_CODE` (from `.env`) are marked ✔ verified, can post official alerts (pinned at the top for 3 days) and can remove false posts. Each post with a location is tagged with our 30-day fire forecast and the live weather there. Messages are saved in `.data/messages.jsonl` (git-ignored, never served). It's a prototype that no real agency watches, so the page always tells people to call their emergency number first. It needs `server.py`; on Vercel it would need a database, so there it only shows a notice.

**On Vercel**
1. Vercel → your project → Settings → Environment Variables → add `FIRMS_MAP_KEY` with the key (and `OPENWEATHER_KEY` if you use it).
2. Deploy this folder. `api/hotspots.js`, `api/status.js` and `api/weather.js` become `/api/hotspots`, `/api/status` and `/api/weather` automatically. `.vercelignore` keeps `.env` and `server.py` from being uploaded.

**Keep the key secret:** `.gitignore` stops `.env` from going to GitHub. Never paste the key into `index.html`. `server.py` only serves `index.html` and `README.md`, so the key file can't be downloaded through it.

How it picks data: for each sensor, the date range is split into ≤5-day requests (the FIRMS area API rejects more than 5). Each day uses the first product that has data for it, in this order: archive then near-real-time, and for VIIRS S-NPP, then NOAA-20, then NOAA-21 (`MODIS_SP`, `MODIS_NRT`; `VIIRS_SNPP_SP`, `VIIRS_SNPP_NRT`, `VIIRS_NOAA20_SP`, `VIIRS_NOAA20_NRT`, `VIIRS_NOAA21_SP`, `VIIRS_NOAA21_NRT`), so there is one VIIRS satellite per day and no duplicate dots. Coverage comes from `/api/data_availability`. S-NPP near-real-time data stopped on 2026-11-02, so recent VIIRS dots come from NOAA-20. Only vegetation fires are kept (`type = 0`), so volcanoes, gas flares and offshore detections are dropped.

## Run it (mock only)
- Quickest: download `index.html` and double-click it so it opens in Chrome/Edge/Firefox (needs internet for the map tiles + libraries from cdnjs). It will NOT work inside an in-app file preview, because previews block outside scripts and map tiles.
- Street basemap: Esri World Street Map (no key). Satellite basemap: NASA GIBS VIIRS true color (no key). Libraries load from cdn.jsdelivr.net.
- The website itself needs **no API keys**. The only key in the project is the FIRMS MAP_KEY, and it lives in the backend's `.env` file.
- Better (avoids browser file:// quirks): `python -m http.server 5500` in this folder → open http://localhost:5500

## What the site does (maps to the challenge)
| Challenge asks for | Where it is |
|---|---|
| Select a region | Draw box on map, type W/S/E/N, or presets |
| Select a date range | From / To (filters map, stats, CSV) |
| Burning activity calendar, early 2000s → now | Year × month heatmap |
| Harmonized MODIS + VIIRS | "Harmonized" view + yearly-totals chart + method card |
| Compare current month to historical median | KPI card + "Anomaly" view + season-profile chart |
| Accessible color ramps | ColorBrewer YlOrRd (sequential) and PuOr (diverging), both colorblind-safe |
| Downloadable CSV | Hotspots CSV (raw rows) + monthly calendar CSV |
| Street map for responders | OSM streets basemap, NASA GIBS VIIRS satellite basemap, 3D tilt |

## Data contract (what the backend must return)
Switch `CONFIG.DATA_MODE` from `'mock'` to `'api'` and set `CONFIG.API_BASE`. The frontend then calls two endpoints.
`bbox` is always `west,south,east,north` in decimal degrees.

### 1. `GET /monthly?bbox=W,S,E,N`
```json
{
  "rows": [
    {"year": 2001, "month": 1, "modis_count": 52, "viirs_count": null, "harmonized_count": 168},
    {"year": 2012, "month": 1, "modis_count": 48, "viirs_count": 151, "harmonized_count": 151}
  ],
  "method": {
    "units": "VIIRS-equivalent hotspots",
    "ratio_by_month": [2.9, 3.3, 3.5, 3.5, 4.0, 4.2, 3.9, 3.6, 3.5, 3.0, 3.0, 2.9],
    "fit_years": [2012, 2018],
    "test_years": [2019, 2026],
    "median_abs_pct_error": 8.4
  }
}
```
- One row per month from 2001-01 to the last complete month, in order.
- `viirs_count` is `null` before VIIRS existed (2012).
- `harmonized_count` = the number the calendar colors by. The frontend never computes harmonization itself; that lives in the backend pipeline.
- If you harmonize in MODIS units instead of VIIRS units, just change `units`. The labels follow it.

### 2. `GET /hotspots?bbox=W,S,E,N&start=YYYY-MM-DD&end=YYYY-MM-DD&limit=5000`
```json
{
  "rows": [
    {"latitude": 28.41, "longitude": -81.22, "acq_date": "2026-03-14", "acq_time": "1812",
     "instrument": "VIIRS", "satellite": "S-NPP", "confidence": "n", "frp": 7.3, "daynight": "D"}
  ],
  "sampled": false,
  "total": 1834,
  "grid": {"cell": 0.01, "fields": ["west", "south", "count", "frp_sum", "frp_max"],
           "cells": [[-81.23, 28.41, 3, 15.2, 7.3]]}
}
```
- Columns are named exactly like FIRMS CSV columns, so the backend can mostly pass FIRMS rows straight through.
- `confidence` is 0–100 for MODIS and `l`/`n`/`h` for VIIRS (that's how FIRMS does it).
- If there are more rows than `limit`, return a random sample and set `sampled: true` + the real `total`.
- `grid` (optional) counts **every** detection, not just the sample, in squares of `cell` degrees (picked so the box is about 200 squares across). Each cell is `[west, south, count, frp_sum, frp_max]`. When `sampled` is true, the map draws these squares instead of dots, and the sample dots appear once you zoom in.
- Note: this CSV has raw detections from *both* sensors, so after 2012 the same fire can appear once as MODIS and once as VIIRS. That's expected, and the `instrument` column tells them apart.

### CORS
If the backend is FastAPI on another port/domain, enable CORS:
```python
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])
```

## Things NOT in the frontend on purpose
- **No FIRMS API key.** The key stays in the backend's `.env`, never in this file or in GitHub. Anything in a website's JavaScript is public.
- **No harmonization math beyond the mock.** The mock's method (monthly VIIRS÷MODIS ratio fit on 2012–2018, tested on 2019→) is only a placeholder that shows how the method card and error KPI will look.

## Ideas for v2 (if time)
- Early-warning panel: last 7 days from FIRMS NRT, compared to the same week's median.
- "Click a calendar cell → map jumps to that month."
- Shareable links (encode bbox + dates in the URL).

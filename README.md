# MavBAC: Burning Activity Calendar + Fire Connect

NASA Space Apps 2026 project. MavBAC turns NASA's satellite fire detections into a calendar of burning activity that you can compare across 2012 to today, even though NASA switched satellites (MODIS to VIIRS) along the way. It also forecasts how likely a wildfire is in the next 30 days, and explains why. **Fire Connect** is a live board where the public, forecasters and first responders can share fire reports, questions and official alerts.

## Try it

| | Link |
|---|---|
| **MavBAC website** | **https://movbac.onrender.com** |
| **Fire Connect (web page)** | https://movbac.onrender.com/connect.html |
| **Fire Connect app (phone)** | **https://movbac.onrender.com/app/** |

> The site runs on a free server that sleeps when nobody has used it for 15 minutes. If it is slow to open, wait about a minute and it will wake up.

### Put the Fire Connect app on your home screen

No app store needed. Open the app link on your phone and add it like any other app:

**iPhone / iPad (Safari)**
1. Open **https://movbac.onrender.com/app/** in **Safari** (it has to be Safari, not Chrome).
2. Tap the **Share** button (the square with an arrow pointing up).
3. Scroll down and tap **Add to Home Screen**, then **Add**.
4. Open **Fire Connect** from your home screen. Tap **Tap to begin** to start the intro with sound.

**Android (Chrome)**
1. Open **https://movbac.onrender.com/app/** in **Chrome**.
2. Tap the **⋮** menu (top right), then **Add to Home screen** or **Install app**.
3. Confirm, then open **Fire Connect** from your home screen.

To get the newest version later, close the app completely and open it again.

### What's inside
- **Burning activity calendar:** monthly fire activity for any area, harmonized so old MODIS counts and new VIIRS counts are on one scale.
- **Map:** real NASA FIRMS fire detections, a 30-day fire-likelihood forecast (machine learning), and a 3D view (Google Photorealistic 3D at city zoom).
- **Wildfire outlook:** pick any place to see how likely a wildfire is in the next 30 days and the reasons: past wildfires and their causes, fire season, climate and this week's weather.
- **Fire Connect:** post a fire or smoke report with your location, ask questions, and see official alerts from verified forecasters and responders. **It is a prototype that no emergency service watches. In an emergency, call your local emergency number first.**

Detailed VIIRS data and the ML forecast cover California, Australia, Brazil and Africa, chosen for their frequent, large wildfires. We plan to extend this to the whole world; that mainly needs bigger servers.

---

# For developers

The website is `index.html` (MavBAC) and `connect.html` (Fire Connect), served by `server.py` (Python standard library only). The phone app is an Expo app in `mavbac-connect/` (kept out of this repo); `python build_app.py` builds it for the web into `app/`, which `server.py` serves at `/app/`.

**Hosting:** the live site runs on Render from this repo (`render.yaml`). Every push to `main` redeploys automatically. API keys are set in Render's Environment settings, never in the code.

## Real NASA FIRMS data (uses the MAP_KEY)
The map dots and the hotspot CSV show **real** FIRMS detections (MODIS + VIIRS).

**On your computer**
1. Keep `.env` next to `server.py`. It holds `FIRMS_MAP_KEY=...` and, optionally, `OPENWEATHER_KEY=...` (adds live weather and a 5-day outlook to the map popups; display only, the forecast model doesn't use it).
2. In this folder run `python server.py`, then open **http://localhost:8000** (not the file directly).
3. The badge should say **LIVE NASA FIRMS DATA**. The default range is the last 30 days. Longer ranges are capped at 366 days and take longer, because FIRMS only allows 5 days per request.
4. Big example: pick the preset **Australia: Black Summer (Sep 2019–Feb 2020)**. That is about 1.4 million detections and 74 FIRMS requests, so the first load takes roughly 30 seconds to a few minutes depending on your internet. Archive answers are then saved (gzipped) in `.cache/` (about 40 MB for that example), and reloading takes about 5 seconds. You can delete `.cache/` any time. It is git-ignored and never served.

**Real calendar + Wildfire outlook**
The calendar, charts, KPIs and harmonization box use real NASA FIRMS data from `data/monthly_1deg.json.gz` (monthly MODIS counts per 1° square for the whole world, plus VIIRS for California, Australia, Brazil and Africa, 2012 to last month). Rebuild it after new downloads with `python ml/monthly.py`. `server.py` serves it at `/api/monthly?bbox=W,S,E,N` (harmonized with a per-month VIIRS/MODIS ratio fitted on 2012-2018 and tested on 2019 onwards).
The **Wildfire outlook** card (`/api/outlook?lat=..&lon=..`) rates the next 30 days for any place and lists the reasons: fire history in that square (how many years burned at this time of year, fire season, trend, last detection), the ML forecast, the long-term climate for the month (NASA POWER, no key) and this week's weather vs normal (OpenWeather). The weather adjustment is a stated rule (about 15% per factor), not part of the trained model. In the USA the verdict is about **reported wildfires** (NIFC WFIGS incident records, 2015 on, public, no key): how often a wildfire over 100 acres started within 50 km at this time of year, the wildfire season, and who/what starts fires there (lightning vs. people: arson, debris burning, equipment, campfires, power lines...). Satellite detections, which also include planned and farm burns, are shown as supporting evidence. Outside the USA there's no open incident record, so the verdict uses satellite fire detections and says so. These two routes exist only in `server.py`, not in the Vercel functions.

**3D view (map button under + / −)**
Tilts the map over whatever spot you're looking at. Zoomed out: terrain (AWS Terrain Tiles) with the fire heat map. Zoomed in to a city (zoom 12+): 3D buildings. Optional **Google Photorealistic 3D Tiles**: enable the *Map Tiles API* in Google Cloud, create an API key restricted to that API, and add `GOOGLE_MAPS_KEY=...` to `.env`. The browser loads tiles from `server.py` at `/v1/3dtiles/...`, which adds the key (it never reaches the browser) and stores nothing. Each visitor can start at most 30 new 3D sessions an hour, to protect billing. If the tiles don't load, the page falls back to the standard 3D view.

**Fire Connect page (`/connect.html`)**
A live message board for forecasters, first responders and the public. Anyone can post a fire/smoke report (with a map pin) or a question. Forecasters and responders who enter `RESPONDER_CODE` (from `.env`) are marked verified, can post official alerts (pinned at the top for 3 days) and can remove false posts. Each post with a location is tagged with our 30-day fire forecast and the live weather there. Messages are saved in `.data/messages.jsonl` (git-ignored, never served). It's a prototype that no real agency watches, so the page always tells people to call their emergency number first. It needs `server.py`; on Vercel it would need a database, so there it only shows a notice.

**On Vercel**
1. Vercel → your project → Settings → Environment Variables → add `FIRMS_MAP_KEY` with the key (and `OPENWEATHER_KEY` if you use it).
2. Deploy this folder. `api/hotspots.js`, `api/status.js` and `api/weather.js` become `/api/hotspots`, `/api/status` and `/api/weather` automatically. `.vercelignore` keeps `.env` and `server.py` from being uploaded.

**Keep the key secret:** `.gitignore` stops `.env` from going to GitHub. Never paste the key into `index.html`. `server.py` only serves the site's own public files, so the key file can't be downloaded through it.

How it picks data: for each sensor, the date range is split into ≤5-day requests (the FIRMS area API rejects more than 5). Each day uses the first product that has data for it, in this order: archive then near-real-time, and for VIIRS S-NPP, then NOAA-20, then NOAA-21 (`MODIS_SP`, `MODIS_NRT`; `VIIRS_SNPP_SP`, `VIIRS_SNPP_NRT`, `VIIRS_NOAA20_SP`, `VIIRS_NOAA20_NRT`, `VIIRS_NOAA21_SP`, `VIIRS_NOAA21_NRT`), so there is one VIIRS satellite per day and no duplicate dots. Coverage comes from `/api/data_availability`. S-NPP near-real-time data stopped on 2026-11-02, so recent VIIRS dots come from NOAA-20. Only vegetation fires are kept (`type = 0`), so volcanoes, gas flares and offshore detections are dropped.

## What the site does (maps to the challenge)
| Challenge asks for | Where it is |
|---|---|
| Select a region | Draw box on map, type W/S/E/N, or presets |
| Select a date range | From / To (filters map, stats, CSV) |
| Burning activity calendar, early 2000s → now | Year × month heatmap, 2012 → now (older MODIS years can be added with `ml/monthly.py`) |
| Harmonized MODIS + VIIRS | "Harmonized" view + yearly-totals chart + method card |
| Compare current month to historical median | KPI card + "Anomaly" view + season-profile chart |
| Accessible color ramps | ColorBrewer YlOrRd (sequential) and PuOr (diverging), both colorblind-safe |
| Downloadable CSV | Hotspots CSV (raw rows) + monthly calendar CSV |
| Street map for responders | OSM streets basemap, NASA GIBS VIIRS satellite basemap, 3D tilt |

"""
Local server for the Burning Activity Calendar.

  python server.py      → open http://localhost:8000

Serves index.html AND a small API that calls NASA FIRMS with your MAP_KEY.
The key is read from the .env file in this folder and never reaches the browser.
Standard-library only: no pip install needed (Python 3.9+).
"""
import csv, gzip, hashlib, heapq, hmac, io, json, math, os, random, re, threading, time
import http.client, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", 8000))
BASE = os.environ.get("FIRMS_BASE", "https://firms.modaps.eosdis.nasa.gov/api")
MAX_DAYS = 366          # longest date range we'll fetch in one go
CHUNK = 5               # FIRMS area API allows 1–5 days per request (it rejects more with HTTP 400)
# For each instrument: FIRMS products in order of preference. For every day we use the FIRST
# product whose availability window covers that day, so there is one satellite per instrument per
# day (no duplicate dots). _SP = archive "standard processing", _NRT = near-real-time.
# VIIRS falls back to NOAA-20 / NOAA-21 when S-NPP has no data (e.g. S-NPP NRT ended 2026-11-02).
SENSORS = {
    "MODIS": ["MODIS_SP", "MODIS_NRT"],
    "VIIRS": ["VIIRS_SNPP_SP", "VIIRS_SNPP_NRT", "VIIRS_NOAA20_SP", "VIIRS_NOAA20_NRT",
              "VIIRS_NOAA21_SP", "VIIRS_NOAA21_NRT"],
}
SAT_NAMES = {"T": "Terra", "A": "Aqua", "N": "S-NPP", "N20": "NOAA-20", "N21": "NOAA-21"}


def load_env():
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
KEY = os.environ.get("FIRMS_MAP_KEY", "").strip()
OW_KEY = os.environ.get("OPENWEATHER_KEY", "").strip()   # optional: live weather in map popups
GOOGLE_KEY = os.environ.get("GOOGLE_MAPS_KEY", "").strip()  # optional: Google Photorealistic 3D Tiles (Map Tiles API)
GTILE_PATH = re.compile(r"^/v1/3dtiles/[A-Za-z0-9_\-./]+\Z")
G3D_SESSIONS_PER_HOUR = 30      # new 3D sessions (billed root requests) allowed per visitor per hour
_g3d_roots = {}
_g3d_lock = threading.Lock()
_g3d_conn = threading.local()   # one kept-open HTTPS connection to Google per server thread (no new TLS handshake per tile)


def google_fetch(path_qs):
    """GET a Google tile over a reused connection. Returns (status, content type, cache-control, body)."""
    for attempt in (0, 1):
        c = getattr(_g3d_conn, "c", None)
        if c is None:
            c = _g3d_conn.c = http.client.HTTPSConnection("tile.googleapis.com", timeout=60)
        try:
            c.request("GET", path_qs, headers={"User-Agent": "burning-activity-calendar/1.0"})
            r = c.getresponse()
            return r.status, r.getheader("Content-Type", "application/octet-stream"), r.getheader("Cache-Control"), r.read()
        except (http.client.HTTPException, OSError):
            c.close(); _g3d_conn.c = None          # stale kept-open connection: retry once on a fresh one
            if attempt:
                raise

PUBLIC_FILES = {"/", "/index.html", "/connect.html", "/theme.css", "/theme.js", "/README.md",
                # Fire Connect as an installable home-screen app
                "/manifest.webmanifest", "/sw.js", "/icons/apple-touch-icon.png", "/icons/icon-192.png",
                "/icons/icon-512.png", "/icons/icon-maskable-512.png"}
PREDICTION_FILE = re.compile(r"^/predictions/[a-z0-9_]+\.json\Z")   # forecast maps made by ml/train.py

CACHE_DIR = os.path.join(ROOT, ".cache")   # downloaded FIRMS answers (gzip). Safe to delete.
NRT_TTL = 3600                               # re-download near-real-time answers after 1 hour
_avail = {"t": 0, "data": {}}


def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "burning-activity-calendar/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:   # keep FIRMS's explanation (e.g. "Invalid day range")
        body = e.read().decode("utf-8", "replace").strip()[:200]
        raise RuntimeError(f"HTTP {e.code}: {body.replace(KEY, '<key>') if KEY else body}") from None


def availability():
    """Which dates each FIRMS product covers: {data_id: (min_date, max_date)}."""
    if time.time() - _avail["t"] < 6 * 3600 and _avail["data"]:
        return _avail["data"]
    data = {}
    try:
        text = http_get(f"{BASE}/data_availability/csv/{KEY}/all", timeout=30)
        for r in csv.DictReader(io.StringIO(text)):
            try:
                data[r["data_id"]] = (date.fromisoformat(r["min_date"]), date.fromisoformat(r["max_date"]))
            except (KeyError, ValueError):
                pass
    except Exception as e:
        print("  ! data_availability failed:", e)
    if data:                                   # don't cache a failure for 6 hours
        _avail.update(t=time.time(), data=data)
    return data


def pick_product(products, d, avail):
    """First product whose [min_date, max_date] covers day d (None if none has data that day)."""
    if not avail:                              # availability unknown → assume archive lags ~3 months
        return products[0] if d <= date.today() - timedelta(days=90) else products[1]
    for p in products:
        lo, hi = avail.get(p, (None, None))
        if lo and lo <= d <= hi:
            return p
    return None


def plan_requests(start, end, avail):
    """Split [start,end] into ≤CHUNK-day requests per instrument, each using one FIRMS product."""
    one = timedelta(days=1)
    jobs = []
    for inst, products in SENSORS.items():
        d = start
        while d <= end:
            src = pick_product(products, d, avail)
            if src is None:
                d += one
                continue
            stop = d
            while stop + one <= end and (stop - d).days + 1 < CHUNK and pick_product(products, stop + one, avail) == src:
                stop += one
            jobs.append((inst, src, d, (stop - d).days + 1))
            d = stop + one
    return jobs


def norm(r, inst):
    t = str(r.get("acq_time", "")).zfill(4)
    sat = r.get("satellite", "")
    sat = SAT_NAMES.get(sat, sat)
    try:
        frp = float(r.get("frp") or 0)
    except ValueError:
        frp = 0.0
    conf = r.get("confidence", "")
    if inst == "MODIS":
        try:
            conf = int(float(conf))
        except ValueError:
            pass
    return {
        "latitude": float(r["latitude"]), "longitude": float(r["longitude"]),
        "acq_date": r["acq_date"], "acq_time": t, "instrument": inst, "satellite": sat,
        "confidence": conf, "frp": frp, "daynight": r.get("daynight", ""),
    }


def cache_path(part):
    return os.path.join(CACHE_DIR, hashlib.sha1(part.encode()).hexdigest() + ".csv.gz")


def firms_csv(part):
    """FIRMS area CSV for 'SOURCE/AREA/DAYS/DATE'. Archive (_SP) answers never change, so they are
    kept on disk forever; near-real-time answers are reused for NRT_TTL seconds."""
    path = cache_path(part)
    try:
        if os.path.exists(path) and ("_SP/" in part or time.time() - os.path.getmtime(path) < NRT_TTL):
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return f.read()
    except OSError:
        pass                                   # broken cache file → just download again
    text = http_get(f"{BASE}/area/csv/{KEY}/{part}")
    if text.lstrip().lower().startswith("latitude"):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = f"{path}.{threading.get_ident()}.tmp"
            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as f:
                f.write(text)
            os.replace(tmp, path)
        except OSError as e:
            print("  ! could not write cache:", e)
    return text


def grid_cell(bbox):
    """Grid square size (degrees) so the area is about 200 squares across."""
    span = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
    for s in (0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1.0):
        if s >= span / 200:
            return s
    return 1.0


def fetch_chunk(job, area, cell, limit):
    """Download + summarize one request. Returns every detection's contribution to the grid, plus a
    uniform random sample of ≤limit rows (each row gets a random key; we keep the smallest keys)."""
    inst, src, d, days = job
    part = f"{src}/{area}/{days}/{d.isoformat()}"
    try:
        text = firms_csv(part)
    except Exception as e:
        return None, f"{src} {d}: {e}"
    if not text.lstrip().lower().startswith("latitude"):
        return None, f"{src} {d}: {text.strip()[:120]}"
    rd = csv.reader(io.StringIO(text))
    header = next(rd)
    col = {h: i for i, h in enumerate(header)}
    ilat, ilon, ifrp, itype = col["latitude"], col["longitude"], col.get("frp"), col.get("type")
    rng = random.Random(part)                 # same request → same sample every time
    heap, grid, n = [], {}, 0
    for row in rd:
        if len(row) < len(header):
            continue
        if itype is not None and row[itype] not in ("", "0"):
            continue                           # keep vegetation fires only (drop volcanoes, gas flares, offshore)
        try:
            lat, lon = float(row[ilat]), float(row[ilon])
        except ValueError:
            continue
        try:
            frp = float(row[ifrp]) if ifrp is not None and row[ifrp] else 0.0
        except ValueError:
            frp = 0.0
        n += 1
        k = (math.floor(lon / cell), math.floor(lat / cell))
        g = grid.get(k)
        if g is None:
            grid[k] = [1, frp, frp]
        else:
            g[0] += 1; g[1] += frp
            if frp > g[2]:
                g[2] = frp
        key = rng.random()
        if len(heap) < limit:
            heapq.heappush(heap, (-key, n, row))
        elif key < -heap[0][0]:
            heapq.heapreplace(heap, (-key, n, row))
    return (inst, header, heap, grid, n), None


def get_hotspots(bbox, start, end, limit):
    if (end - start).days + 1 > MAX_DAYS:
        start = end - timedelta(days=MAX_DAYS - 1)
    area = ",".join(f"{v:.4f}" for v in bbox)
    cell = grid_cell(bbox)
    jobs = plan_requests(start, end, availability())
    sample, grid, total, warnings, seq = [], {}, 0, [], 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(fetch_chunk, j, area, cell, limit) for j in jobs]
        for fut in as_completed(futures):      # merge each answer as soon as it arrives
            res, err = fut.result()
            if err:
                warnings.append(err)
            if not res:
                continue
            inst, header, heap, g, n = res
            total += n
            for k, (c, s, m) in g.items():
                G = grid.get(k)
                if G is None:
                    grid[k] = [c, s, m]
                else:
                    G[0] += c; G[1] += s
                    if m > G[2]:
                        G[2] = m
            for negkey, _, row in heap:        # keep the `limit` smallest random keys overall
                seq += 1
                item = (negkey, seq, inst, header, row)
                if len(sample) < limit:
                    heapq.heappush(sample, item)
                elif negkey > sample[0][0]:
                    heapq.heapreplace(sample, item)
    rows = []
    for _, _, inst, header, row in sample:
        try:
            rows.append(norm(dict(zip(header, row)), inst))
        except (KeyError, ValueError):
            pass
    rows.sort(key=lambda r: (r["acq_date"], r["acq_time"]))
    cells = [[round(ix * cell, 4), round(iy * cell, 4), c, round(s, 1), round(m, 1)]
             for (ix, iy), (c, s, m) in grid.items()]
    return {"rows": rows, "sampled": total > len(rows), "total": total, "source": "NASA FIRMS",
            "start": start.isoformat(), "end": end.isoformat(),
            "requests": len(jobs), "warnings": warnings[:10],
            "grid": {"cell": cell, "fields": ["west", "south", "count", "frp_sum", "frp_max"], "cells": cells}}


_weather = {}   # (lat, lon) rounded to 0.1° -> (time, answer); OpenWeather's free plan allows 60 calls/min


def get_weather(lat, lon):
    """Current conditions + a 5-day summary from OpenWeather (shown in popups; the ML model doesn't use it)."""
    lat, lon = round(lat, 1), round(lon, 1)
    hit = _weather.get((lat, lon))
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    q = f"lat={lat}&lon={lon}&units=metric&appid={OW_KEY}"
    try:
        now = json.loads(http_get(f"https://api.openweathermap.org/data/2.5/weather?{q}", timeout=20))
        fc = json.loads(http_get(f"https://api.openweathermap.org/data/2.5/forecast?{q}", timeout=20))
    except Exception as e:
        raise RuntimeError(str(e).replace(OW_KEY, "<key>")) from None
    steps = fc.get("list", [])
    out = {
        "place": now.get("name") or "",
        "description": (now.get("weather") or [{}])[0].get("description", ""),
        "temp_c": now["main"]["temp"],
        "humidity": now["main"]["humidity"],
        "wind_ms": now.get("wind", {}).get("speed", 0),
        "gust_ms": now.get("wind", {}).get("gust"),
        "rain_1h_mm": now.get("rain", {}).get("1h", 0),
        "next5": {
            "max_temp_c": max((s["main"]["temp_max"] for s in steps), default=None),
            "min_humidity": min((s["main"]["humidity"] for s in steps), default=None),
            "max_wind_ms": max((s.get("wind", {}).get("speed", 0) for s in steps), default=None),
            "rain_mm": round(sum(s.get("rain", {}).get("3h", 0) for s in steps), 1),
        },
        "source": "OpenWeather",
    }
    if len(_weather) > 2000:
        _weather.clear()
    _weather[(lat, lon)] = (time.time(), out)
    return out


# ---------------------------------------------------------------------------------------------------
# Connect page: a shared message board for forecasters, first responders and civilians.
# Messages are kept in .data/messages.jsonl (git-ignored, never served). Forecasters and responders
# prove who they are with RESPONDER_CODE from .env; only they can post official alerts or delete posts.
# It is a prototype: no real agency watches it, so the page always says to call the emergency number.
# ---------------------------------------------------------------------------------------------------
RESPONDER_CODE = os.environ.get("RESPONDER_CODE", "").strip()
MSG_FILE = os.path.join(ROOT, ".data", "messages.jsonl")
MSG_KINDS = {"alert", "report", "question", "reply"}
MSG_ROLES = {"civilian", "forecaster", "responder"}
_msgs, _msg_lock, _last_post = [], threading.Lock(), {}


def load_messages():
    if os.path.exists(MSG_FILE):
        for line in open(MSG_FILE, encoding="utf-8"):
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("delete"):                         # a deletion record
                for x in _msgs:
                    if x["id"] == m["delete"]:
                        x["deleted"] = True
            else:
                _msgs.append(m)


def save_line(obj):
    os.makedirs(os.path.dirname(MSG_FILE), exist_ok=True)
    with open(MSG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


_pred = {"t": 0, "files": []}


def forecast_at(lat, lon):
    """Fire chance (next 30 days) from the ML forecast square containing this point, if any."""
    pdir = os.path.join(ROOT, "predictions")
    if time.time() - _pred["t"] > 600:
        files = []
        try:
            index = json.load(open(os.path.join(pdir, "index.json"), encoding="utf-8"))
            for e in index:
                f = json.load(open(os.path.join(pdir, e["region"] + ".json"), encoding="utf-8"))
                lookup = {(round(w / f["cell"]), round(s / f["cell"])): (p, u) for w, s, p, u in f["cells"]}
                files.append((e["region"] == "world", f["name"], f["cell"], f["valid_from"], f["valid_to"], lookup))
        except Exception:
            pass
        files.sort(key=lambda x: x[0])                   # regional forecasts before the world one
        _pred.update(t=time.time(), files=files)
    for _, name, cell, vf, vt, lookup in _pred["files"]:
        hit = lookup.get((math.floor(lon / cell), math.floor(lat / cell)))
        if hit:
            return {"p": hit[0], "usual": hit[1], "model": name, "valid_from": vf, "valid_to": vt}
    return None


def public(m):
    return {k: v for k, v in m.items() if k != "ip"}


def post_message(body, ip):
    """Validate and store one message. Returns (http_code, answer)."""
    kind, role = body.get("kind"), body.get("role", "civilian")
    text = str(body.get("text", "")).strip()
    name = str(body.get("name", "")).strip()[:40] or "Anonymous"
    if kind not in MSG_KINDS or role not in MSG_ROLES or not 1 <= len(text) <= 1000:
        return 400, {"error": "need kind, role and a message of 1-1000 characters"}
    verified = False
    if role != "civilian":
        if not RESPONDER_CODE or not hmac.compare_digest(str(body.get("code", "")), RESPONDER_CODE):
            return 403, {"error": "Wrong or missing responder code. Forecasters and responders need the code to post as verified."}
        verified = True
    if kind == "alert" and not verified:
        return 403, {"error": "Only verified forecasters and responders can post official alerts."}
    now = time.time()
    if not verified and now - _last_post.get(ip, 0) < 10:
        return 429, {"error": "Please wait a few seconds between posts."}
    m = {"kind": kind, "role": role, "verified": verified, "name": name, "text": text, "time": int(now)}
    try:
        lat, lon = float(body["lat"]), float(body["lon"])
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            m["lat"], m["lon"] = round(lat, 4), round(lon, 4)
    except (KeyError, TypeError, ValueError):
        pass
    if kind == "reply":
        try:
            m["reply_to"] = int(body["reply_to"])
        except (KeyError, TypeError, ValueError):
            return 400, {"error": "a reply needs reply_to"}
    if kind == "report" and "lat" not in m:
        return 400, {"error": "A fire report needs a location: click the map or use 'my location'."}
    if "lat" in m:                                       # context for whoever reads it
        m["forecast"] = forecast_at(m["lat"], m["lon"])
        if OW_KEY:
            try:
                m["weather"] = get_weather(m["lat"], m["lon"])
            except Exception:
                pass
    with _msg_lock:
        if kind == "reply" and not any(x["id"] == m["reply_to"] for x in _msgs):
            return 400, {"error": "that message no longer exists"}
        m["id"] = (_msgs[-1]["id"] + 1) if _msgs else 1
        m["ip"] = ip
        _msgs.append(m)
        _last_post[ip] = now
        save_line(m)
    return 200, public(m)


def delete_message(body):
    if not RESPONDER_CODE or not hmac.compare_digest(str(body.get("code", "")), RESPONDER_CODE):
        return 403, {"error": "only verified responders can remove posts"}
    with _msg_lock:
        for m in _msgs:
            if m["id"] == body.get("id"):
                m["deleted"] = True
                save_line({"delete": m["id"], "time": int(time.time())})
                return 200, {"ok": True}
    return 404, {"error": "no such message"}


load_messages()


# ---------------------------------------------------------------------------------------------------
# Real monthly calendar (/api/monthly) and the Wildfire outlook widget (/api/outlook).
# data/monthly_1deg.json.gz is built by ml/monthly.py from the FIRMS downloads: monthly MODIS counts per
# 1° square (whole world) and VIIRS counts where a VIIRS region covers it.
# ---------------------------------------------------------------------------------------------------
MONTHLY_FILE = os.path.join(ROOT, "data", "monthly_1deg.json.gz")
FIT_YEARS = (2012, 2018)
_monthly = {"data": None, "mtime": 0}
_monthly_lock = threading.Lock()


def _mid(s):
    y, m = map(int, s.split("-"))
    return y * 12 + m - 1


def monthly_data():
    try:
        mt = os.path.getmtime(MONTHLY_FILE)
    except OSError:
        return None
    with _monthly_lock:
        if _monthly["data"] is None or mt != _monthly["mtime"]:
            with gzip.open(MONTHLY_FILE, "rt", encoding="utf-8") as f:
                d = json.load(f)
            d["_m0"], d["_m1"] = _mid(d["first_month"]), _mid(d["last_complete"])
            d["_v0"] = _mid(d["viirs_first_month"]) if d.get("viirs_first_month") else 10 ** 9
            _monthly.update(data=d, mtime=mt)
        return _monthly["data"]


def _viirs_covered(d, ix, iy):
    lon, lat = ix + 0.5, iy + 0.5
    return any(W <= lon <= E and S <= lat <= N for W, S, E, N in d["viirs_cover"])


def get_monthly(bbox):
    """Monthly MODIS, VIIRS and harmonized counts for a box (1° squares, weighted by how much of each the box covers)."""
    d = monthly_data()
    if d is None:
        return None
    W, S, E, N = bbox
    n = d["_m1"] - d["_m0"] + 1
    cells = []
    for ix in range(math.floor(W), math.ceil(E)):
        ow = min(E, ix + 1) - max(W, ix)
        for iy in range(math.floor(S), math.ceil(N)):
            oh = min(N, iy + 1) - max(S, iy)
            if ow > 0 and oh > 0:
                cells.append((ix, iy, ow * oh))
    has_viirs = bool(cells) and all(_viirs_covered(d, ix, iy) for ix, iy, _ in cells)
    mod, vir = [0.0] * n, [0.0] * n
    for ix, iy, w in cells:
        k = f"{ix},{iy}"
        a = d["modis"].get(k)
        if a:
            for i, v in enumerate(a):
                if v:
                    mod[i] += w * v
        b = d["viirs"].get(k) if has_viirs else None
        if b:
            for i, v in enumerate(b):
                if v:
                    vir[i] += w * v
    m0, v0 = d["_m0"], d["_v0"]
    world = d["world"]["ratio_by_month"]
    fallback = sum(r for r in world if r) / max(1, sum(1 for r in world if r))
    ratio, src = [], []
    for mo in range(12):
        sm = sv = 0.0
        if has_viirs:
            for i in range(n):
                m = m0 + i
                if m % 12 == mo and FIT_YEARS[0] <= m // 12 <= FIT_YEARS[1] and m >= v0:
                    sm += mod[i]
                    sv += vir[i]
        if sm >= 30 and sv > 0:
            ratio.append(sv / sm)
            src.append("area")
        else:
            ratio.append(world[mo] or fallback)
            src.append("world")
    errs, rows = [], []
    for i in range(n):
        m = m0 + i
        y, mo = divmod(m, 12)
        v = round(vir[i]) if has_viirs and m >= v0 else None
        if v is not None and y > FIT_YEARS[1] and vir[i] > 20:
            errs.append(abs(mod[i] * ratio[mo] - vir[i]) / vir[i])
        rows.append({"year": y, "month": mo + 1, "modis_count": round(mod[i]), "viirs_count": v,
                     "harmonized_count": v if v is not None else round(mod[i] * ratio[mo])})
    errs.sort()
    area_ratio = src.count("area") == 12
    err = round(errs[len(errs) // 2] * 100, 1) if errs else d["world"].get("median_abs_pct_error")
    last = d["_m1"]
    method = {
        "units": "VIIRS-equivalent hotspots",
        "ratio_by_month": [round(r, 2) for r in ratio],
        "fit_years": list(FIT_YEARS),
        "test_years": [FIT_YEARS[1] + 1, last // 12],
        "median_abs_pct_error": err,
        "error_scope": "this area" if errs else "world (per 1° square and month)",
        "ratio_source": "this area" if area_ratio else ("mixed" if "area" in src else "world"),
        "viirs_here": has_viirs,
        "resolution_deg": d["cell"],
        "first_month": d["first_month"], "last_complete": d["last_complete"],
        "viirs_regions": d["viirs_cover"], "built": d.get("built"),
    }
    return {"rows": rows, "method": method}


_climate = {}


def climate_normals(lat, lon):
    """NASA POWER long-term monthly climate (no key). Cached per 0.5°."""
    key = (round(lat * 2) / 2, round(lon * 2) / 2)
    if key in _climate:
        return _climate[key]
    # Climatology T2M_MAX is the month's record high, so the typical daily high = mean + half the daily range.
    url = ("https://power.larc.nasa.gov/api/temporal/climatology/point?parameters=T2M,T2M_RANGE,RH2M,PRECTOTCORR,WS2M"
           f"&community=AG&longitude={key[1]}&latitude={key[0]}&format=JSON")
    try:
        p = json.loads(http_get(url, timeout=40))["properties"]["parameter"]
        mons = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
        out = {k: [p[k][m] for m in mons] for k in ("T2M", "T2M_RANGE", "RH2M", "PRECTOTCORR", "WS2M")}
        out["HIGH"] = [t + r / 2 for t, r in zip(out["T2M"], out["T2M_RANGE"])]
    except Exception:
        out = None
    if len(_climate) > 3000:
        _climate.clear()
    _climate[key] = out
    return out


MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Reported wildfires with their causes (USA only): NIFC "WFIGS" incident records, 2014 on. Public, no key.
# This is what separates real wildfires (and who/what started them) from the satellite's "any fire seen".
WFIGS = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
         "WFIGS_Incident_Locations/FeatureServer/0/query")
RADIUS_KM = 50
_wfigs = {}
HUMAN_CAUSE_NAMES = {"Incendiary": "arson", "Debris/Open Burning": "debris/yard burning",
                     "Debris and open burning": "debris/yard burning", "Equipment": "equipment and vehicles",
                     "Camping": "campfires", "Smoking": "cigarettes", "Utilities": "power lines",
                     "Firearms/Weapons": "firearms", "Railroad": "railroads", "Fireworks": "fireworks",
                     "Children": "children playing with fire", "Other Human Cause": "other human causes",
                     "Misuse of fire by a minor": "children playing with fire"}


def _in_usa(lat, lon):
    return (24 <= lat <= 50 and -125 <= lon <= -66) or (51 <= lat <= 72 and -170 <= lon <= -129) \
        or (18.5 <= lat <= 22.5 and -161 <= lon <= -154)


def reported_wildfires(lat, lon):
    """Wildfires (and prescribed fires) reported to US agencies within RADIUS_KM, summarised. None outside the USA."""
    if not _in_usa(lat, lon):
        return None
    key = (round(lat * 4) / 4, round(lon * 4) / 4)
    hit = _wfigs.get(key)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    dlat = RADIUS_KM / 111.0
    dlon = RADIUS_KM / (111.0 * max(0.2, math.cos(math.radians(lat))))
    rows, off = [], 0
    while off < 30000:
        q = urllib.parse.urlencode({
            "where": "IncidentTypeCategory IN ('WF','RX')",
            "geometry": f"{lon - dlon},{lat - dlat},{lon + dlon},{lat + dlat}",
            "geometryType": "esriGeometryEnvelope", "inSR": 4326, "outSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "IncidentTypeCategory,FireCause,FireCauseGeneral,FireDiscoveryDateTime,IncidentSize,IncidentName",
            "returnGeometry": "true", "orderByFields": "OBJECTID", "resultOffset": off, "resultRecordCount": 2000,
            "f": "json"})
        j = json.loads(http_get(f"{WFIGS}?{q}", timeout=60))
        if "error" in j:
            raise RuntimeError(str(j["error"])[:200])
        feats = j.get("features", [])
        for f in feats:
            g = f.get("geometry") or {}
            if "x" not in g:
                continue
            dy = (g["y"] - lat) * 111.0
            dx = (g["x"] - lon) * 111.0 * math.cos(math.radians(lat))
            if dx * dx + dy * dy <= RADIUS_KM ** 2 and f["attributes"].get("FireDiscoveryDateTime"):
                rows.append(f["attributes"])
        if not j.get("exceededTransferLimit") or not feats:
            break
        off += len(feats)

    today = date.today()
    wf = [r for r in rows if r.get("IncidentTypeCategory") == "WF"]
    rx = sum(1 for r in rows if r.get("IncidentTypeCategory") == "RX")
    for r in wf:
        r["_d"] = date.fromtimestamp(r["FireDiscoveryDateTime"] / 1000)
        r["_a"] = float(r.get("IncidentSize") or 0)
    years = list(range(2015, today.year))                      # complete years with full reporting
    window = {today.month, today.month % 12 + 1}
    per_year = {y: 0 for y in years}
    per_year_big = {y: 0 for y in years}
    win_years = win_years_big = 0
    win_counts = []
    for y in years:
        inw = [r for r in wf if r["_d"].year == y and r["_d"].month in window]
        win_counts.append(len(inw))
        win_years += bool(inw)
        win_years_big += any(r["_a"] >= 100 for r in inw)
    by_month = [0] * 12
    for r in wf:
        if r["_d"].year in per_year:
            per_year[r["_d"].year] += 1
            per_year_big[r["_d"].year] += r["_a"] >= 100
            by_month[r["_d"].month - 1] += 1
    causes = {"Human": 0, "Natural": 0}
    general = {}
    for r in wf:
        c = r.get("FireCause")
        if c in causes:
            causes[c] += 1
        g = r.get("FireCauseGeneral")
        if g and g not in ("Undetermined", "Investigated but Undetermined", "Cause and Origin Not Identified"):
            name = "lightning" if g == "Lightning" else HUMAN_CAUSE_NAMES.get(g, g.lower())
            general[name] = general.get(name, 0) + 1
    sizes = sorted(r["_a"] for r in wf)
    recent = sorted([r for r in wf if (today - r["_d"]).days <= 365], key=lambda r: -r["_a"])[:3]
    out = {
        "radius_km": RADIUS_KM, "since": 2015, "years": len(years),
        "wildfires": sum(per_year.values()), "per_year": round(sum(per_year.values()) / max(1, len(years)), 1),
        "big_per_year": round(sum(per_year_big.values()) / max(1, len(years)), 1),
        "prescribed_reported": rx,
        "window_years_any": win_years, "window_years_big": win_years_big,
        "window_mean": round(sum(win_counts) / max(1, len(win_counts)), 1),
        "median_acres": sizes[len(sizes) // 2] if sizes else None,
        "acres_total": round(sum(sizes)),
        "human": causes["Human"], "natural": causes["Natural"],
        "causes": sorted(general.items(), key=lambda kv: -kv[1])[:6],
        "by_month": by_month,
        "recent": [{"name": (r.get("IncidentName") or "Unnamed").title(), "date": r["_d"].isoformat(),
                    "acres": round(r["_a"]), "cause": r.get("FireCause") or "Unknown"} for r in recent],
        "last_12_months": sum(1 for r in wf if (today - r["_d"]).days <= 365),
    }
    if len(_wfigs) > 2000:
        _wfigs.clear()
    _wfigs[key] = (time.time(), out)
    return out
FIRE_MIN = 3     # "a fire month" = at least 3 detections in the square (same threshold as the ML model)


def get_outlook(lat, lon):
    """Is this place susceptible to (seasonal) wildfire over the next 30 days, and why?"""
    d = monthly_data()
    if d is None:
        return None
    ix, iy = math.floor(lon), math.floor(lat)
    rows = get_monthly([ix, iy, ix + 1, iy + 1])["rows"]
    today = date.today()
    window = [today.month - 1, today.month % 12]                     # this month and next (0-based)
    by_year = {}
    for r in rows:
        by_year.setdefault(r["year"], {})[r["month"] - 1] = r["harmonized_count"]
    full_years = sorted(y for y, ms in by_year.items() if len(ms) == 12)
    n_years = len(full_years)
    win_counts = [sum(by_year[y].get(m, 0) for m in window) for y in full_years]
    years_fire = sum(1 for c in win_counts if c >= FIRE_MIN)
    freq = years_fire / n_years if n_years else 0.0

    def med(a):
        a = sorted(a)
        return 0 if not a else (a[len(a) // 2] if len(a) % 2 else (a[len(a) // 2 - 1] + a[len(a) // 2]) / 2)
    profile = [med([by_year[y][m] for y in full_years]) for m in range(12)]
    yearly = sum(profile) or 0
    top = max(profile) if profile else 0
    season = [m for m in range(12) if top > 0 and profile[m] >= 0.5 * top and profile[m] >= FIRE_MIN]
    in_season = any(m in season for m in window)
    win_share = (sum(profile[m] for m in window) / yearly) if yearly else 0
    totals = {y: sum(by_year[y].values()) for y in full_years}
    recent = full_years[-3:]
    earlier = full_years[:-3]
    trend = None
    if recent and earlier:
        a = sum(totals[y] for y in recent) / len(recent)
        b = med([totals[y] for y in earlier])           # median year, so one disaster year doesn't dominate
        trend = (a - b) / b if b > 0 else None
    worst = max(totals, key=totals.get) if totals else None
    last_fire = d["last_fire"].get(f"{ix},{iy}")

    reasons = []

    def add(factor, effect, weight, title, detail):
        reasons.append({"factor": factor, "effect": effect, "weight": weight, "title": title, "detail": detail})

    wname = f"{MONTH_NAMES[window[0]]}–{MONTH_NAMES[window[1]]}"

    # 0. Reported wildfires and their causes (USA): the real wildfire record, incl. human-caused fires
    try:
        rw = reported_wildfires(lat, lon)
    except Exception:
        rw = None
    sat_w = "medium" if rw else "high"          # with a real wildfire record, satellite counts are supporting evidence
    if rw:
        yrs = rw["years"]
        p_any, p_big = rw["window_years_any"] / yrs, rw["window_years_big"] / yrs
        if rw["wildfires"] == 0:
            add("reported", "lowers", "high", f"No wildfires reported within {RADIUS_KM} km since {rw['since']}",
                "US fire agencies (NIFC) have no wildfire records near this place.")
        else:
            eff = "raises" if p_any >= 0.5 else "lowers" if p_any <= 0.2 else "neutral"
            add("reported", eff, "high",
                f"Wildfires reported nearby in {wname}: {rw['window_years_any']} of the last {yrs} years",
                f"Within {RADIUS_KM} km, US fire agencies recorded {rw['wildfires']:,} wildfires since {rw['since']} "
                f"(about {rw['per_year']:g} a year; {rw['big_per_year']:g} a year grew past 100 acres). Most are small: "
                f"half were {rw['median_acres']:g} acres or less. In {wname}: at least one wildfire in {rw['window_years_any']} "
                f"of {yrs} years, one over 100 acres in {rw['window_years_big']} of {yrs}.")
            bm = rw["by_month"]
            top_m = max(bm) if bm else 0
            peak = [m for m in range(12) if top_m and bm[m] >= 0.5 * top_m]
            share = sum(bm[m] for m in window) / max(1, sum(bm))
            if peak:
                in_peak = any(m in peak for m in window)
                add("wf_season", "raises" if in_peak else "lowers", "high",
                    "This is wildfire season here" if in_peak else
                    f"Outside the usual wildfire season ({', '.join(MONTH_NAMES[m] for m in peak)})",
                    f"{share:.0%} of the wildfires reported here started in {wname}. "
                    f"Busiest months: {', '.join(MONTH_NAMES[m] for m in peak)}.")
            known = rw["human"] + rw["natural"]
            if known >= 5:
                hs = rw["human"] / known
                title = (f"People start most wildfires here ({hs:.0%})" if hs >= 0.6 else
                         f"Lightning starts most wildfires here ({1 - hs:.0%})" if hs <= 0.4 else
                         f"People and lightning start wildfires here about equally ({hs:.0%} human)")
                listed = ", ".join(f"{n} {c}" for n, c in rw["causes"]) if rw["causes"] else ""
                add("causes", "neutral", "high", title,
                    (f"Recorded causes: {listed}. " if listed else "") +
                    ("Human-caused fires can start on any dry, windy day: yard-debris burning, equipment sparks, "
                     "arson, cigarettes, campfires and fireworks." if hs >= 0.4 else
                     "Lightning fires cluster in the thunderstorm season after dry spells."))
            if rw["recent"]:
                big = rw["recent"][0]
                busier = rw["last_12_months"] > 1.5 * rw["per_year"]
                add("recent_wf", "raises" if busier else "neutral", "medium",
                    f"Largest wildfire nearby in the last 12 months: {big['name']} ({big['acres']:,} acres)",
                    f"{rw['last_12_months']} wildfires reported within {RADIUS_KM} km in the last 12 months"
                    f"{' (busier than usual)' if busier else ''}. Largest: " +
                    "; ".join(f"{r['name']}, {r['acres']:,} acres, {r['date']}, cause: {r['cause'].lower()}" for r in rw["recent"]) + ".")

    # 1. Satellite fire history (any fire the satellite sees)
    if n_years == 0 or yearly == 0 and not any(win_counts):
        add("history", "lowers", sat_w, "Satellites have seen no fire here",
            f"No satellite fire detections in this ~110 km square since {d['first_month'][:4]}.")
    else:
        eff = "raises" if freq >= 0.5 else "neutral" if freq >= 0.2 else "lowers"
        rx_note = (f" {rw['prescribed_reported']} planned burns are in the agency records here, and most planned and farm burns "
                   "are never listed there." if rw else "")
        add("history", eff, sat_w, f"Satellites saw fire here in {years_fire} of {n_years} years at this time of year",
            f"In {wname}, this ~110 km square had at least {FIRE_MIN} satellite fire detections in {years_fire} of {n_years} years "
            f"({full_years[0]}–{full_years[-1]}). This counts every fire seen from space: wildfires, but also planned "
            f"(prescribed) burns, farm burning and some industrial heat.{rx_note}")
    # 2. Seasonality of satellite-seen fire
    if season:
        sname = ", ".join(MONTH_NAMES[m] for m in season)
        add("season", "raises" if in_season else "lowers", sat_w,
            "Peak season for fire activity" if in_season else "Outside the peak season for fire activity",
            f"Satellite-seen fire peaks in {sname} in this square. {wname} usually holds {win_share:.0%} of the year's fire activity.")
    # 3. Trend and worst year
    if trend is not None:
        eff = "raises" if trend > 0.2 else "lowers" if trend < -0.2 else "neutral"
        title = (f"Fire activity {'up' if trend > 0 else 'down'} {abs(trend):.0%} in recent years" if abs(trend) >= 0.05
                 else "Fire activity about the same as usual in recent years")
        add("trend", eff, "medium", title,
            f"Average yearly detections {recent[0]}–{recent[-1]} vs a typical (median) year in {earlier[0]}–{earlier[-1]}. "
            f"Worst year: {worst} ({totals[worst]:,} detections).")
    if last_fire:
        days = (today - date.fromisoformat(last_fire)).days
        add("recent", "raises" if days <= 30 else "neutral", "medium" if days <= 30 else "low",
            f"Last fire detected {days} day{'s' if days != 1 else ''} ago" if days > 0 else "Fire detected today",
            f"Most recent satellite fire detection in this square: {last_fire}.")

    # 4. ML forecast
    fc = forecast_at(lat, lon)
    if fc:
        diff = fc["p"] - fc["usual"]
        eff = ("raises" if diff > 0.05 and fc["p"] > fc["usual"] * 1.2 else
               "lowers" if diff < -0.05 and fc["p"] < fc["usual"] * 0.8 else "neutral")
        add("model", eff, sat_w, f"ML forecast: {round(fc['p'] * 100)}% chance of satellite-seen fire in 30 days",
            f"{fc['model']} model ({fc['valid_from']} → {fc['valid_to']}); usual for this time of year: {round(fc['usual'] * 100)}%.")

    # 5. Long-term climate for this month (NASA POWER)
    clim = climate_normals(lat, lon)
    mo = window[0]
    if clim:
        rain = clim["PRECTOTCORR"]
        rank = sorted(range(12), key=lambda m: rain[m]).index(mo) + 1     # 1 = driest
        eff = "raises" if rank <= 4 else "lowers" if rank >= 9 else "neutral"
        add("climate", eff, "medium",
            f"{MONTH_NAMES[mo]} is usually {'one of the driest months' if rank <= 4 else 'one of the wettest months' if rank >= 9 else 'an average month for rain'} here",
            f"Long-term average for {MONTH_NAMES[mo]} (NASA POWER): {rain[mo]:.1f} mm rain/day (rank {rank} of 12, 1 = driest), "
            f"typical highs {clim['HIGH'][mo]:.0f}°C, humidity {clim['RH2M'][mo]:.0f}%.")

    # 6. This week's weather vs normal (OpenWeather)
    wx = None
    if OW_KEY:
        try:
            wx = get_weather(lat, lon)
        except Exception:
            wx = None
    nudge = 0
    if wx:
        n5 = wx["next5"]
        bits, up, down = [], 0, 0
        if clim:
            normal_rain = clim["PRECTOTCORR"][mo] * 5
            if n5["rain_mm"] < 0.3 * normal_rain and normal_rain >= 2:
                up += 1; bits.append(f"only {n5['rain_mm']} mm of rain forecast vs ~{normal_rain:.0f} mm normal")
            elif n5["rain_mm"] > 2 * normal_rain + 5:
                down += 1; bits.append(f"{n5['rain_mm']} mm of rain forecast vs ~{normal_rain:.0f} mm normal")
            if n5["max_temp_c"] is not None and n5["max_temp_c"] > clim["HIGH"][mo] + 3:
                up += 1; bits.append(f"hotter than normal (up to {n5['max_temp_c']:.0f}°C vs {clim['HIGH'][mo]:.0f}°C)")
            elif n5["max_temp_c"] is not None and n5["max_temp_c"] < clim["HIGH"][mo] - 3:
                down += 1; bits.append(f"cooler than normal (up to {n5['max_temp_c']:.0f}°C)")
        if n5["min_humidity"] is not None and n5["min_humidity"] < 25:
            up += 1; bits.append(f"very dry air (humidity down to {n5['min_humidity']}%)")
        elif n5["min_humidity"] is not None and n5["min_humidity"] > 70:
            down += 1; bits.append(f"humid air (humidity stays above {n5['min_humidity']}%)")
        if n5["max_wind_ms"] is not None and n5["max_wind_ms"] * 3.6 >= 30:
            up += 1; bits.append(f"strong winds up to {round(n5['max_wind_ms'] * 3.6)} km/h (fires spread faster)")
        nudge = up - down
        eff = "raises" if nudge > 0 else "lowers" if nudge < 0 else "neutral"
        mixed = up and down
        add("weather", eff, "medium",
            "This week's weather " + ("favours fire" if nudge > 0 else "works against fire" if nudge < 0
                                       else "is mixed" if mixed else "is about normal"),
            ("Next 5 days: " + "; ".join(bits) + ".") if bits else
            f"Next 5 days: up to {n5['max_temp_c']:.0f}°C, humidity down to {n5['min_humidity']}%, rain {n5['rain_mm']} mm.")

    # Verdict: the ML chance where we have one (it already learned from history + season), otherwise the
    # historical frequency; this week's weather nudges it (a simple, stated rule, not a trained model).
    if rw:
        # USA: the verdict is about real wildfires (agency records), not every fire a satellite sees.
        base = rw["window_years_big"] / rw["years"]
        chance = max(0.0, min(0.99, base * (1 + 0.15 * nudge) + (0.03 * nudge if base == 0 and nudge > 0 else 0)))
        level = "Very high" if chance >= 0.6 else "High" if chance >= 0.3 else "Moderate" if chance >= 0.1 else "Low"
        what = f"Chance of a wildfire over 100 acres within {RADIUS_KM} km in the next 30 days"
        basis = (f"Happened in {rw['window_years_big']} of {rw['years']} years in {wname}; "
                 f"smaller wildfires in {rw['window_years_any']} of {rw['years']} years")
        pyr = rw["per_year"]
        suscept = (f"Wildfires are common around here (about {pyr:g} a year), mostly small" if pyr >= 20 else
                   f"Wildfires happen here most years (about {pyr:g} a year)" if pyr >= 3 else
                   "Occasional wildfires around here" if pyr >= 0.5 else "Wildfires are rare around here")
        base_source = "reported wildfires"
    else:
        base = fc["p"] if fc else freq
        chance = max(0.0, min(0.99, base * (1 + 0.15 * nudge)))
        level = "Very high" if chance >= 0.7 else "High" if chance >= 0.4 else "Moderate" if chance >= 0.15 else "Low"
        what = "Chance of fire of any kind (wildfire, farm or planned burn) seen by satellite in this ~110 km square in 30 days"
        basis = f"{'ML forecast' if fc else 'fire history'} {round(base * 100)}%"
        suscept = ("Fire seen almost every year at this time" if freq >= 0.7 else "Fire often seen at this time of year" if freq >= 0.4
                   else "Fire sometimes seen at this time of year" if freq >= 0.15 else "Fire rarely seen at this time of year")
        base_source = "ML forecast" if fc else "fire history"
    order = {"high": 0, "medium": 1, "low": 2}
    reasons.sort(key=lambda r: order[r["weight"]])
    return {
        "lat": round(lat, 4), "lon": round(lon, 4), "square": [ix, iy, ix + 1, iy + 1],
        "place": (wx or {}).get("place", ""),
        "window": {"from": today.isoformat(), "to": (today + timedelta(days=30)).isoformat(), "months": [MONTH_NAMES[m] for m in window]},
        "verdict": {"level": level, "chance": round(chance, 3), "base": round(base, 3), "what": what, "basis": basis,
                    "base_source": base_source, "weather_nudge": nudge},
        "reported": rw,
        "susceptibility": {"label": suscept, "years_with_fire": years_fire, "years": n_years, "freq": round(freq, 3)},
        "profile": profile, "season_months": [MONTH_NAMES[m] for m in season], "window_index": window,
        "reasons": reasons,
        "notes": (["Reported wildfires: NIFC WFIGS incident records (USA, 2015 on). Small brush fires handled only by a "
                   "city fire department may be missing."] if rw else
                  ["Outside the USA there is no open wildfire-incident record, so this uses satellite fire detections."]) + [
                  "Satellites see every fire: wildfires, planned (prescribed) burns, farm burning and some industrial heat.",
                  "House and other building fires (like a kitchen fire) aren't wildfires and aren't in these records. "
                  "Wildfires that people start (arson, yard-debris burning, equipment, campfires, cigarettes, fireworks, "
                  "power lines) are counted.",
                  "The weather adjustment is a simple rule (±15% per factor), not part of the trained model."],
    }


class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".webmanifest": "application/manifest+json"}

    def end_headers(self):
        # The service worker and the app manifest must never be stale, or installed phones miss updates.
        if self.path.split("?")[0] in ("/sw.js", "/manifest.webmanifest"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def client_ip(self):
        """The visitor's address. On a host like Render every request arrives from its proxy, so with
        TRUST_PROXY=1 the real address is taken from the proxy's headers (never trusted when run locally)."""
        if os.environ.get("TRUST_PROXY") == "1":
            for h in ("CF-Connecting-IP", "True-Client-IP"):
                v = (self.headers.get(h) or "").strip()
                if v:
                    return v
            xff = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            if xff:
                return xff
        return self.client_address[0]

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        if self.path.startswith("/api/messages"):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/status":
            return self.send_json(200, {"live": bool(KEY), "weather": bool(OW_KEY), "google3d": bool(GOOGLE_KEY),
                                        "today": date.today().isoformat()})
        if u.path == "/api/monthly":
            q = urllib.parse.parse_qs(u.query)
            try:
                bbox = [float(x) for x in q["bbox"][0].split(",")]
                assert len(bbox) == 4 and bbox[0] < bbox[2] and bbox[1] < bbox[3]
            except Exception:
                return self.send_json(400, {"error": "need bbox=W,S,E,N"})
            out = get_monthly(bbox)
            if out is None:
                return self.send_json(503, {"error": "monthly dataset missing: run python ml/monthly.py"})
            return self.send_json(200, out)
        if u.path == "/api/outlook":
            q = urllib.parse.parse_qs(u.query)
            try:
                lat, lon = float(q["lat"][0]), float(q["lon"][0])
                assert -90 <= lat <= 90 and -180 <= lon <= 180
            except Exception:
                return self.send_json(400, {"error": "need lat=..&lon=.."})
            out = get_outlook(lat, lon)
            if out is None:
                return self.send_json(503, {"error": "monthly dataset missing: run python ml/monthly.py"})
            return self.send_json(200, out)
        if u.path == "/api/messages":
            q = urllib.parse.parse_qs(u.query)
            try:
                since = int(q.get("since", ["0"])[0])
            except ValueError:
                since = 0
            with _msg_lock:
                new = [public(m) for m in _msgs if m["id"] > since][-500:]
                removed = [m["id"] for m in _msgs if m.get("deleted")]
            return self.send_json(200, {"messages": new, "removed": removed,
                                        "verified_posting": bool(RESPONDER_CODE), "now": int(time.time())})
        if u.path == "/api/weather":
            if not OW_KEY:
                return self.send_json(500, {"error": "OPENWEATHER_KEY missing from .env"})
            q = urllib.parse.parse_qs(u.query)
            try:
                lat, lon = float(q["lat"][0]), float(q["lon"][0])
                assert -90 <= lat <= 90 and -180 <= lon <= 180
            except Exception:
                return self.send_json(400, {"error": "need lat=..&lon=.."})
            try:
                return self.send_json(200, get_weather(lat, lon))
            except Exception as e:
                return self.send_json(502, {"error": str(e)[:200]})
        if u.path == "/api/hotspots":
            if not KEY:
                return self.send_json(500, {"error": "FIRMS_MAP_KEY missing from .env"})
            q = urllib.parse.parse_qs(u.query)
            try:
                bbox = [float(x) for x in q["bbox"][0].split(",")]
                assert len(bbox) == 4 and bbox[0] < bbox[2] and bbox[1] < bbox[3]
                start = date.fromisoformat(q["start"][0])
                end = min(date.fromisoformat(q["end"][0]), date.today())
                limit = max(1, min(int(q.get("limit", ["5000"])[0]), 50000))
                assert start <= end
            except Exception:
                return self.send_json(400, {"error": "need bbox=W,S,E,N&start=YYYY-MM-DD&end=YYYY-MM-DD"})
            t = time.time()
            out = get_hotspots(bbox, start, end, limit)
            print(f"  hotspots {out['start']}..{out['end']}: {out['total']} rows, "
                  f"{out['requests']} FIRMS requests, {time.time() - t:.1f}s")
            return self.send_json(200, out)
        # Google Photorealistic 3D Tiles, passed through with the key added here so it never reaches the
        # browser. Nothing is stored (Google's terms don't allow caching the tiles).
        if GTILE_PATH.match(u.path):
            return self.google_tile(u)
        # Only the website's own public files are served. Everything else (.env with the key,
        # server.py, ...) is refused, whatever tricks are in the URL.
        path = urllib.parse.unquote(u.path)
        if path not in PUBLIC_FILES and not PREDICTION_FILE.match(path):
            return self.send_error(404)
        return super().do_GET()

    def google_tile(self, u):
        if not GOOGLE_KEY:
            return self.send_error(404)
        q = urllib.parse.parse_qs(u.query)
        params = {"session": q["session"][0]} if "session" in q else {}
        if u.path.endswith("/root.json"):          # each root request starts a billed 3D session
            ip, now = self.client_ip(), time.time()
            with _g3d_lock:
                recent = [t for t in _g3d_roots.get(ip, []) if now - t < 3600]
                if len(recent) >= G3D_SESSIONS_PER_HOUR:
                    return self.send_json(429, {"error": "Too many new 3D sessions this hour; try again later."})
                _g3d_roots[ip] = recent + [now]
        params["key"] = GOOGLE_KEY
        try:
            status, ctype, cache, body = google_fetch(u.path + "?" + urllib.parse.urlencode(params))
        except Exception as e:
            return self.send_json(502, {"error": f"Google 3D tiles unreachable: {type(e).__name__}"})
        if status != 200:
            return self.send_json(status, {"error": f"Google 3D tiles answered HTTP {status} (is the Map Tiles API enabled for this key?)"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        # This server never stores tiles. The browser may keep them only as long as Google's own header allows;
        # root.json (which starts a billed session) is never reused.
        self.send_header("Cache-Control", "no-store" if u.path.endswith("/root.json") or not cache else cache)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):                      # CORS preflight for the message board
        if not self.path.startswith("/api/messages"):
            return self.send_error(404)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "3600")
        self.end_headers()

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path not in ("/api/messages", "/api/messages/delete"):
            return self.send_error(404)
        try:
            n = int(self.headers.get("Content-Length", 0))
            assert 0 < n <= 8192
            body = json.loads(self.rfile.read(n))
            assert isinstance(body, dict)
        except Exception:
            return self.send_json(400, {"error": "send a JSON object (max 8 KB)"})
        if u.path == "/api/messages/delete":
            return self.send_json(*delete_message(body))
        return self.send_json(*post_message(body, self.client_ip()))

    def log_message(self, fmt, *args):
        if "/api/" not in str(args[0] if args else ""):
            return  # keep the console quiet for static files
        super().log_message(fmt, *args)


if __name__ == "__main__":
    print("Burning Activity Calendar")
    print("  FIRMS key:", "loaded from .env" if KEY else "MISSING (add FIRMS_MAP_KEY=... to .env)")
    print("  OpenWeather key:", "loaded from .env" if OW_KEY else "not set (popups show no live weather)")
    print("  Google 3D tiles:", "on (GOOGLE_MAPS_KEY in .env)" if GOOGLE_KEY else "off (add GOOGLE_MAPS_KEY=... to .env)")
    print(f"  Open http://localhost:{PORT}   (Ctrl+C to stop)")
    # HOST=0.0.0.0 in .env lets phones on the same Wi-Fi reach it (for the Fire Connect app in Expo Go).
    host = os.environ.get("HOST", "127.0.0.1").strip() or "127.0.0.1"
    if host != "127.0.0.1":
        print(f"  Listening on {host}:{PORT}: other devices on your network can open it.")
    ThreadingHTTPServer((host, PORT), Handler).serve_forever()

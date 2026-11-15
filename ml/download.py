"""
Step 1: download the raw data for a region.

  python ml/download.py firms   <region>   -> ml/data/<region>/firms_daily.npz  (detections per square per day)
  python ml/download.py weather <region>   -> ml/data/<region>/weather/*.json

FIRMS: every VIIRS vegetation-fire detection since 2012, reusing server.py's FIRMS code and its .cache/
       (so the FIRMS key still only lives in .env). Each answer is added straight into daily counts per
       grid square, so even Africa (hundreds of millions of detections) fits in memory.
       Run `firms` before `weather`: weather is only fetched for squares that actually burn.
Weather: NASA POWER daily weather + soil wetness per POWER grid pixel (0.5° x 0.625°), no key needed.
Both steps can be stopped and re-run; finished pieces are skipped (FIRMS answers are cached in .cache/).
"""
import io, json, os, sys, threading, time, urllib.error, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import server                                            # noqa: E402  (FIRMS helpers + key from .env)
import grid                                              # noqa: E402
from regions import FIRST_DAY, REGIONS                   # noqa: E402

POWER_PARAMS = ["T2M_MAX", "RH2M", "PRECTOTCORR", "WS2M_MAX", "GWETROOT", "GWETTOP"]
DAY0 = date.fromisoformat(FIRST_DAY) - timedelta(days=400)   # every daily array starts here
BUDGET = 4300                                                # stay below FIRMS's 5000 per 10 minutes


def log(*a, sep=" "):
    print(time.strftime("%H:%M:%S"), *a, sep=sep, flush=True)


def data_dir(region):
    d = os.path.join(HERE, "data", region)
    os.makedirs(d, exist_ok=True)
    return d


class FirmsBudget:
    """FIRMS counts 'transactions' per key per 10 minutes, and big answers cost more (a 5-day Africa
    request ≈ 40). Check the key's real usage counter and pause while it's near the limit."""
    def __init__(self):
        self.lock, self.checked, self.used, self.fails = threading.Lock(), 0.0, 0, 0

    def status(self):
        url = f"https://firms.modaps.eosdis.nasa.gov/mapserver/mapkey_status/?MAP_KEY={server.KEY}"
        return int(json.loads(server.http_get(url, timeout=30))["current_transactions"])

    def wait(self):
        with self.lock:
            while True:
                if time.time() - self.checked > 4:
                    try:
                        self.used, self.checked, self.fails = self.status(), time.time(), 0
                    except Exception as e:
                        # Can't tell → assume busy, but only for a few tries: a check that keeps failing
                        # must not stall the download forever (it did once, for 7 hours).
                        self.fails += 1
                        log(f"  usage check failed ({self.fails}): {str(e)[:120]}")
                        self.used, self.checked = (BUDGET if self.fails <= 3 else 0), time.time()
                if self.used < BUDGET:
                    self.used += 40                                     # count our own request until next check
                    return
                log(f"  FIRMS limit nearly used ({self.used}/5000 in the last 10 min), pausing 30 s")
                time.sleep(30)
                self.checked = 0


def download_firms(region):
    R = REGIONS[region]
    ix0, iy0, nx, ny = grid.bbox_grid(R)
    end = date.today() - timedelta(days=1)
    D = (end - DAY0).days + 1
    count = np.zeros((D, nx * ny), np.float32)
    frp = np.zeros((D, nx * ny), np.float32)
    total = api_fill(region, date.fromisoformat(FIRST_DAY), end, count, frp)
    save_fires(region, count, frp, end, total)


def save_fires(region, count, frp, end, total):
    R = REGIONS[region]
    ix0, iy0, nx, ny = grid.bbox_grid(R)
    out = grid.fires_path(region)
    tmp = out + ".tmp.npz"
    np.savez_compressed(tmp, count=count, frp=frp, day0=str(DAY0), last_day=str(end),
                        cell=R["cell"], ix0=ix0, iy0=iy0, nx=nx, ny=ny)
    os.replace(tmp, out)
    log(f"saved {total:,} detections -> {out}")


def add_detections(df, R, count, frp, lock=None):
    """Add a FIRMS table (latitude, longitude, acq_date, frp[, type]) into the daily per-square arrays."""
    ix0, iy0, nx, ny = grid.bbox_grid(R)
    c, D = R["cell"], count.shape[0]
    if "type" in df:
        df = df[df["type"].fillna(0) == 0]        # vegetation fires only (drop volcanoes, gas flares, offshore)
    if df.empty:
        return 0
    di = (pd.to_datetime(df["acq_date"]) - pd.Timestamp(DAY0)).dt.days.values
    i = np.floor(df["longitude"].values / c).astype(int) - ix0
    j = np.floor(df["latitude"].values / c).astype(int) - iy0
    ok = (i >= 0) & (i < nx) & (j >= 0) & (j < ny) & (di >= 0) & (di < D)
    k = i[ok] * ny + j[ok]
    if lock:
        lock.acquire()
    try:
        np.add.at(count, (di[ok], k), 1)
        np.add.at(frp, (di[ok], k), df["frp"].fillna(0).values[ok])
    finally:
        if lock:
            lock.release()
    return int(ok.sum())


def api_fill(region, start, end, count, frp):
    """Fetch [start, end] from the FIRMS area API (5-day requests) into the arrays. Returns detections added."""
    R = REGIONS[region]
    data_dir(region)
    sensor = R.get("sensor", "VIIRS")
    server.SENSORS = {sensor: server.SENSORS[sensor]}     # labels use one sensor (consistent over time)
    # Which product covers which dates. Without it the plan falls back to a guess that leaves holes
    # (S-NPP NRT ended 2026-11-02; the archive ends 2026-07-31), so wait until FIRMS answers.
    for _ in range(15):
        avail = server.availability()
        if avail:
            break
        log("  waiting for FIRMS data_availability (rate limit?) ...")
        time.sleep(60)
    else:
        sys.exit("FIRMS data_availability keeps failing; try again in 10 minutes.")
    jobs = server.plan_requests(start, end, avail)
    area = ",".join(f"{v:.4f}" for v in R["bbox"])
    ix0, iy0, nx, ny = grid.bbox_grid(R)
    add_lock, budget = threading.Lock(), FirmsBudget()
    log(f"{region}: {sensor}, {len(jobs)} FIRMS requests, {start} .. {end}, grid {nx}x{ny} of {R['cell']} deg")

    def one(job):
        _, src, d, days = job
        part = f"{src}/{area}/{days}/{d.isoformat()}"
        cached = os.path.exists(server.cache_path(part))
        for attempt in range(12):                 # be patient: up to ~10 minutes of retries
            if not cached:
                budget.wait()
            try:
                text = server.firms_csv(part)
            except Exception as e:
                text = f"error: {e}"
            if text.lstrip().lower().startswith("latitude"):
                break
            cached = False
            if attempt == 11:
                return 0, f"{src} {d}: {text.strip()[:100]}"
            time.sleep(60)
        df = pd.read_csv(io.StringIO(text), usecols=lambda k: k in ("latitude", "longitude", "acq_date", "frp", "type"))
        return add_detections(df, R, count, frp, add_lock), None

    total, done, warnings = 0, 0, []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for fut in as_completed([ex.submit(one, j) for j in jobs]):
            n, err = fut.result()
            total += n
            done += 1
            if err:
                warnings.append(err)
            if done % 50 == 0 or done == len(jobs):
                log(f"  {done}/{len(jobs)} requests, {total:,} detections")
    if warnings:
        log(f"{len(warnings)} requests failed (re-run to retry them), e.g.:", *warnings[:5], sep="\n  ")
    return total


ARCHIVE = "https://firms.modaps.eosdis.nasa.gov/data/country/zips/{sensor}_{year}_all_countries.zip"


def download_firms_archive(region):
    """Whole-world regions: FIRMS's yearly all-countries archive zips (no key, no transaction limit),
    then the area API only for the days after the newest zip."""
    R = REGIONS[region]
    sensor = R.get("sensor", "MODIS").lower()
    zdir = os.path.join(data_dir(region), "zips")
    os.makedirs(zdir, exist_ok=True)
    end = date.today() - timedelta(days=1)
    ix0, iy0, nx, ny = grid.bbox_grid(R)
    D = (end - DAY0).days + 1
    count = np.zeros((D, nx * ny), np.float32)
    frp = np.zeros((D, nx * ny), np.float32)
    total, last = 0, date.fromisoformat(FIRST_DAY) - timedelta(days=1)
    for year in range(date.fromisoformat(FIRST_DAY).year, end.year + 1):
        path = os.path.join(zdir, f"{sensor}_{year}_all_countries.zip")
        if not os.path.exists(path):
            try:
                req = urllib.request.Request(ARCHIVE.format(sensor=sensor, year=year), headers={"User-Agent": "burning-activity-calendar/1.0"})
                with urllib.request.urlopen(req, timeout=600) as r, open(path + ".tmp", "wb") as f:
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
                os.replace(path + ".tmp", path)
            except urllib.error.HTTPError as e:
                log(f"  no archive for {year} (HTTP {e.code}); the API will cover it")
                break
        n_year = 0
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if not name.endswith(".csv"):
                    continue
                with z.open(name) as fh:
                    df = pd.read_csv(fh, usecols=lambda k: k in ("latitude", "longitude", "acq_date", "frp", "type"))
                if df.empty:
                    continue
                last = max(last, date.fromisoformat(str(df["acq_date"].max())[:10]))
                n_year += add_detections(df, R, count, frp)
        total += n_year
        log(f"  {year}: {n_year:,} detections from the archive (newest day so far {last})")
    if last < end:
        total += api_fill(region, last + timedelta(days=1), end, count, frp)
    save_fires(region, count, frp, end, total)


def download_weather(region):
    if not REGIONS[region].get("weather", True):
        log(f"{region}: weather turned off for this region, skipping")
        return
    wdir = os.path.join(data_dir(region), "weather")
    os.makedirs(wdir, exist_ok=True)
    pixels = sorted({grid.power_pixel(lon, lat) for lon, lat in grid.cell_centers(region)})
    start, end = DAY0, date.today()
    log(f"{region}: {len(pixels)} NASA POWER pixels, {start} .. {end}")

    def one(px):
        lon, lat = grid.power_center(px)
        path = os.path.join(wdir, f"{px[0]}_{px[1]}.json")
        if os.path.exists(path):
            return None
        url = ("https://power.larc.nasa.gov/api/temporal/daily/point?parameters=" + ",".join(POWER_PARAMS) +
               f"&community=AG&longitude={lon:.4f}&latitude={lat:.4f}"
               f"&start={start:%Y%m%d}&end={end:%Y%m%d}&format=JSON")
        err = None
        for attempt in range(6):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "burning-activity-calendar/1.0"})
                with urllib.request.urlopen(req, timeout=300) as r:
                    j = json.load(r)
                keep = {"lon": lon, "lat": lat, "elevation": j["geometry"]["coordinates"][2],
                        "parameter": j["properties"]["parameter"]}
                with open(path + ".tmp", "w") as f:
                    json.dump(keep, f)
                os.replace(path + ".tmp", path)
                return None
            except Exception as e:
                err = e
                time.sleep(15 * (attempt + 1))
        return f"{px}: {err}"

    errors, done = [], 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for fut in as_completed([ex.submit(one, px) for px in pixels]):
            done += 1
            if fut.result():
                errors.append(fut.result())
            if done % 100 == 0 or done == len(pixels):
                log(f"  {done}/{len(pixels)} pixels")
    log(f"weather done, {len(errors)} failed", *errors[:5], sep="\n  ")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    what, region = sys.argv[1], sys.argv[2]
    {"firms": download_firms, "firms_archive": download_firms_archive, "weather": download_weather}[what](region)

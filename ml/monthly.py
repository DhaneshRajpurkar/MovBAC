"""
Build the REAL monthly fire dataset behind the calendar, charts and the Wildfire outlook widget.

  python ml/monthly.py              -> data/monthly_1deg.json.gz

Sources (all NASA FIRMS vegetation-fire detections, already downloaded by ml/download.py):
  MODIS  whole world, 1° squares   ml/data/world/firms_daily.npz           (2012 -> today)
         + yearly archive zips      ml/data/world/zips/modis_<year>_*.zip   (any years before 2012 found there)
  VIIRS  S-NPP (NOAA-20/21 fill-in)  ml/data/<california|australia|brazil|africa>/firms_daily.npz (2012 -> today)

Output: per 1° square, monthly MODIS counts and (where a VIIRS region covers it) monthly VIIRS counts,
plus the date of the last detection and a world-level VIIRS/MODIS ratio. server.py (standard library only)
reads it to answer /api/monthly and /api/outlook.
"""
import gzip, json, os, sys, zipfile
from datetime import date, timedelta

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from regions import REGIONS   # noqa: E402

OUT = os.path.join(os.path.dirname(HERE), "data", "monthly_1deg.json.gz")
VIIRS_REGIONS = ["california", "australia", "brazil", "africa"]
FIT = (2012, 2018)


def month_index(d0, D):
    """Month id (year*12+month-1) for each of D days starting at d0."""
    days = pd.date_range(d0, periods=D, freq="D")
    return (days.year * 12 + days.month - 1).values


def monthly_from_npz(path, cell_to_1deg):
    """Daily per-cell counts -> {1° cell: {month_id: count}} and {1° cell: last day with a detection}."""
    z = np.load(path)
    count = z["count"]                                # (D, ncells) float32
    d0 = date.fromisoformat(str(z["day0"]))
    mid = month_index(d0, count.shape[0])
    starts = np.r_[0, np.nonzero(np.diff(mid))[0] + 1]
    per_month = np.add.reduceat(count, starts, axis=0)    # (n_months, ncells)
    month_ids = mid[starts]
    has = count > 0
    last_day = np.where(has.any(axis=0), count.shape[0] - 1 - np.argmax(has[::-1], axis=0), -1)
    del count, has
    out, last = {}, {}
    for k in np.nonzero(per_month.sum(axis=0) > 0)[0]:
        key = cell_to_1deg(int(k))
        series = out.setdefault(key, {})
        for m, v in zip(month_ids, per_month[:, k]):
            if v:
                series[int(m)] = series.get(int(m), 0) + float(v)
        if last_day[k] >= 0:
            dd = (d0 + timedelta(days=int(last_day[k]))).isoformat()
            last[key] = max(last.get(key, ""), dd)
    return out, last, int(month_ids.min()), int(month_ids.max())


def cell_mapper(z, cell):
    ix0, iy0, ny = int(z["ix0"]), int(z["iy0"]), int(z["ny"])
    def f(k):
        i, j = divmod(k, ny)
        return (int(np.floor((ix0 + i) * cell + 1e-9)), int(np.floor((iy0 + j) * cell + 1e-9)))
    return f


def modis_before_2012(zdir, years):
    """Pre-2012 MODIS straight from the yearly archive zips (if they've been downloaded)."""
    out = {}
    for y in years:
        path = os.path.join(zdir, f"modis_{y}_all_countries.zip")
        if not os.path.exists(path):
            continue
        n = 0
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not name.endswith(".csv"):
                    continue
                with zf.open(name) as fh:
                    df = pd.read_csv(fh, usecols=lambda c: c in ("latitude", "longitude", "acq_date", "type"))
                if "type" in df:
                    df = df[df["type"].fillna(0) == 0]
                if df.empty:
                    continue
                dt = pd.to_datetime(df["acq_date"])
                mid = (dt.dt.year * 12 + dt.dt.month - 1).values
                ix = np.floor(df["longitude"].values).astype(int)
                iy = np.floor(df["latitude"].values).astype(int)
                g = pd.DataFrame({"ix": ix, "iy": iy, "m": mid}).value_counts()
                for (a, b, m), c in g.items():
                    s = out.setdefault((int(a), int(b)), {})
                    s[int(m)] = s.get(int(m), 0) + int(c)
                n += len(df)
        print(f"  MODIS {y}: {n:,} detections from the archive", flush=True)
    return out


def main():
    world = os.path.join(HERE, "data", "world", "firms_daily.npz")
    print("MODIS (world, 1°) ...", flush=True)
    with np.load(world) as z:
        wmap = cell_mapper(z, 1.0)
    modis, last, m_first, m_last = monthly_from_npz(world, wmap)

    early = modis_before_2012(os.path.join(HERE, "data", "world", "zips"), range(2001, 2012))
    for key, s in early.items():
        dst = modis.setdefault(key, {})
        for m, v in s.items():
            dst[m] = dst.get(m, 0) + v
    # The world file starts 400 days before 2012 but only holds data from 2012 on (archives start there).
    m_first = min(min(s) for s in early.values()) if early else max(m_first, 2012 * 12)

    viirs, cover = {}, []
    v_first = None
    for r in VIIRS_REGIONS:
        path = os.path.join(HERE, "data", r, "firms_daily.npz")
        if not os.path.exists(path):
            continue
        print(f"VIIRS ({r}) ...", flush=True)
        R = REGIONS[r]
        with np.load(path) as z:
            f = cell_mapper(z, float(z["cell"]))
            first_day = date.fromisoformat(str(z["day0"]))
        s, l, a, _ = monthly_from_npz(path, f)
        for key, ser in s.items():
            dst = viirs.setdefault(key, {})
            for m, v in ser.items():
                dst[m] = dst.get(m, 0) + v
        for key, d in l.items():
            last[key] = max(last.get(key, ""), d)
        cover.append([float(x) for x in R["bbox"]])
        v_first = 2012 * 12 + 1        # Feb 2012: first full month of VIIRS (data starts 2012-01-20)

    # Only complete months
    today = date.today()
    last_complete = today.year * 12 + today.month - 2
    m_last = min(m_last, last_complete)
    months = list(range(m_first, m_last + 1))
    pos = {m: i for i, m in enumerate(months)}

    def dense(s, start=None):
        a = [0] * len(months)
        for m, v in s.items():
            if m in pos and (start is None or m >= start):
                a[pos[m]] = int(round(v))
        return a

    mod_out = {f"{k[0]},{k[1]}": dense(s) for k, s in modis.items() if sum(s.values()) >= 1}
    vir_out = {f"{k[0]},{k[1]}": dense(s, v_first) for k, s in viirs.items() if sum(s.values()) >= 1}

    # World-level VIIRS/MODIS ratio by calendar month, from every 1° square both sensors cover.
    def in_cover(key):
        ix, iy = map(int, key.split(","))
        lon, lat = ix + 0.5, iy + 0.5
        return any(W <= lon <= E and S <= lat <= N for W, S, E, N in cover)
    both = [k for k in mod_out if in_cover(k)]
    ratio, err = [], []
    for mo in range(12):
        sv = sm = 0
        for k in both:
            vs = vir_out.get(k)
            for i, m in enumerate(months):
                if m % 12 == mo and FIT[0] <= m // 12 <= FIT[1] and m >= v_first:
                    sm += mod_out[k][i]
                    sv += vs[i] if vs else 0
        ratio.append(round(sv / sm, 3) if sm else None)
    for k in both:
        vs = vir_out.get(k) or [0] * len(months)
        for i, m in enumerate(months):
            if m // 12 > FIT[1] and vs[i] > 20 and ratio[m % 12]:
                err.append(abs(mod_out[k][i] * ratio[m % 12] - vs[i]) / vs[i])
    test_err = round(float(np.median(err)) * 100, 1) if err else None

    out = {
        "cell": 1.0,
        "first_month": f"{months[0] // 12}-{months[0] % 12 + 1:02d}",
        "last_complete": f"{months[-1] // 12}-{months[-1] % 12 + 1:02d}",
        "viirs_first_month": f"{v_first // 12}-{v_first % 12 + 1:02d}" if v_first else None,
        "viirs_cover": cover,
        "modis": mod_out,
        "viirs": vir_out,
        "last_fire": {f"{k[0]},{k[1]}": d for k, d in last.items()},
        "world": {"ratio_by_month": ratio, "fit_years": list(FIT), "median_abs_pct_error": test_err,
                  "cells_with_both": len(both)},
        "built": today.isoformat(),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with gzip.open(OUT + ".tmp", "wt", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace(OUT + ".tmp", OUT)
    print(f"wrote {OUT}: {len(mod_out):,} MODIS squares, {len(vir_out):,} VIIRS squares, "
          f"{out['first_month']}..{out['last_complete']}, world ratio {ratio}, test error {test_err}%")


if __name__ == "__main__":
    main()

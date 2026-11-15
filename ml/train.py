"""
Step 2: build the training table, train + test the models, and write the map layer.

  python ml/train.py california      -> predictions/california.json  (+ ml/data/california/report.json)

Question the model answers, for every grid square:
  "Will satellites see a fire here (at least LABEL_MIN detections) in the NEXT 30 days?"
using only what is known on the forecast day (past fires, past weather, soil dryness, season).

Honest testing: train on 2013-2020, tune on 2021-2022, score once on 2023→now (years never seen).
Every model is compared with the climatology baseline ("how often did this square burn at this
time of year in the training years"); a model that can't beat it adds nothing.
"""
import json, math, os, sys, time
os.environ.setdefault("LOKY_MAX_CPU_COUNT", str(os.cpu_count()))   # silences a harmless joblib warning on Windows
from datetime import date, timedelta

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import grid                                   # noqa: E402
from regions import FIRST_DAY, REGIONS        # noqa: E402

HORIZON = 30            # days ahead
STEP = 7                # a forecast every 7 days in the training data
LABEL_MIN = 3           # ≥3 detections in 30 days counts as "fire" (1-2 are often false alarms / tiny burns)
TRAIN_END, VAL_END = date(2020, 12, 31), date(2022, 12, 31)
PARAMS = ["T2M_MAX", "RH2M", "PRECTOTCORR", "WS2M_MAX", "GWETROOT", "GWETTOP"]

FEATURE_GROUPS = {      # for the human-readable "what mattered" list
    "Season (time of year)": ["doy_sin", "doy_cos"],
    "Usual fire activity here at this time of year": ["clim"],
    "Recent fires in this square": ["fire_30", "fire_90", "fire_365", "frp_30", "days_since_fire"],
    "Recent fires in neighbouring squares": ["nb_30", "nb_365"],
    "Soil dryness (NASA POWER)": ["gwetroot", "gwettop", "gwetroot_anom"],
    "Rain in the last 30/90 days": ["precip_30", "precip_90", "precip_90_anom"],
    "Heat": ["tmax_30", "tmax_30_anom"],
    "Humidity": ["rh_30"],
    "Wind": ["wind_30"],
    "Elevation": ["elevation"],
}
WEATHER_GROUPS = {"Soil dryness (NASA POWER)", "Rain in the last 30/90 days", "Heat", "Humidity", "Wind", "Elevation"}


def feature_groups(R):
    return {g: fs for g, fs in FEATURE_GROUPS.items() if R.get("weather", True) or g not in WEATHER_GROUPS}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def window_sum(cs, t, n):
    """Sum over days (t-n, t] using a cumulative sum along axis 0 (cs[0] = 0 row)."""
    return cs[t + 1] - cs[max(t + 1 - n, 0)]


def load(region):
    R = REGIONS[region]
    ddir = os.path.join(HERE, "data", region)
    cells = grid.cells(region)
    C = len(cells)

    # --- fires: daily detections per square (made by download.py firms)
    with np.load(grid.fires_path(region)) as z:
        day0 = date.fromisoformat(str(z["day0"]))
        last_day = (date.fromisoformat(str(z["last_day"])) - day0).days
        ix0, iy0, ny = int(z["ix0"]), int(z["iy0"]), int(z["ny"])
        cols = np.array([(ix - ix0) * ny + (iy - iy0) for ix, iy in cells])
        count, frp = z["count"][:, cols], z["frp"][:, cols]
    D = last_day + 1
    log(f"{int(count.sum()):,} detections in {C} squares, {day0 + timedelta(days=400)} .. {day0 + timedelta(days=last_day)}")

    if not R.get("weather", True):
        log("no weather for this region (model uses fire history + season only)")
        return dict(R=R, cells=cells, day0=day0, count=count, frp=frp, W=None, elev=None,
                    last_fire_day=last_day, last_weather=last_day)

    # --- weather per POWER pixel -> per square
    pix = [grid.power_pixel(*c) for c in grid.cell_centers(region)]
    upix = sorted(set(pix))
    where = {p: j for j, p in enumerate(upix)}
    pidx = np.array([where[p] for p in pix])
    W = {p: np.full((D, len(upix)), np.nan, np.float32) for p in PARAMS}
    elev = np.zeros(len(upix), np.float32)
    dayof = {}
    for j, px in enumerate(upix):
        with open(os.path.join(ddir, "weather", f"{px[0]}_{px[1]}.json")) as fh:
            w = json.load(fh)
        elev[j] = w["elevation"]
        keys = list(w["parameter"][PARAMS[0]].keys())
        for k in keys:
            if k not in dayof:
                dayof[k] = (date(int(k[:4]), int(k[4:6]), int(k[6:])) - day0).days
        di = np.array([dayof[k] for k in keys])
        ok = (di >= 0) & (di < D)
        for p in PARAMS:
            v = np.array([w["parameter"][p].get(k, -999) for k in keys], np.float32)
            v[v < -900] = np.nan
            W[p][di[ok], j] = v[ok]
    last_weather = None
    for p in PARAMS:                                          # last day with real data, then forward-fill
        ok = np.where(~np.isnan(W[p]).all(axis=1))[0]
        last = int(ok.max())
        last_weather = last if last_weather is None else min(last_weather, last)
        W[p] = pd.DataFrame(W[p]).ffill().bfill().values.astype(np.float32)
    W = {p: v[:, pidx] for p, v in W.items()}                 # pixel -> square
    log(f"weather from {len(upix)} NASA POWER pixels, real data until {day0 + timedelta(days=last_weather)}")
    return dict(R=R, cells=cells, day0=day0, count=count, frp=frp, W=W, elev=elev[pidx],
                last_fire_day=last_day, last_weather=last_weather)


def build(S):
    """Feature rows for every (forecast day, square). Labels are NaN when the future isn't known yet."""
    count, frp, W, cells = S["count"], S["frp"], S["W"], S["cells"]
    D, C = count.shape
    day0 = S["day0"]
    z = lambda a: np.vstack([np.zeros((1, a.shape[1]), a.dtype), np.cumsum(a, axis=0, dtype=np.float64)])
    cs_n, cs_f = z(count), z(frp)
    cs_w = {p: z(W[p]) for p in ["T2M_MAX", "RH2M", "PRECTOTCORR", "WS2M_MAX"]} if W is not None else None

    # neighbours: sum over the 3x3 block around each square, minus itself
    pos = {c: i for i, c in enumerate(cells)}
    nbrs = [[pos[(ix + dx, iy + dy)] for dx in (-1, 0, 1) for dy in (-1, 0, 1)
             if (dx or dy) and (ix + dx, iy + dy) in pos] for ix, iy in cells]
    nb_mat = np.zeros((C, C), np.float32)
    for i, ns in enumerate(nbrs):
        nb_mat[ns, i] = 1

    # days since the last fire in each square
    last = np.full(C, -10_000, np.int64)
    since = np.zeros((D, C), np.int32)
    for d in range(D):
        last[count[d] > 0] = d
        since[d] = np.minimum(d - last, 2000)

    first = (date.fromisoformat(FIRST_DAY) - day0).days + 365
    now = min(S["last_fire_day"], S["last_weather"])                 # the newest forecast we can issue
    days = list(range(first, now + 1, STEP))
    if days[-1] != now:
        days.append(now)
    rows = []
    for t in days:
        dt = day0 + timedelta(days=t)
        doy = dt.timetuple().tm_yday
        fire_30 = window_sum(cs_n, t, 30)
        fire_365 = window_sum(cs_n, t, 365)
        label = window_sum(cs_n, t + HORIZON, HORIZON) if t + HORIZON <= S["last_fire_day"] else np.full(C, np.nan)
        cols = {
            "t": t, "date": pd.Timestamp(dt), "year": dt.year, "week": min((doy - 1) // 7, 51), "cell": np.arange(C),
            "doy_sin": math.sin(2 * math.pi * doy / 365.25), "doy_cos": math.cos(2 * math.pi * doy / 365.25),
            "fire_30": fire_30, "fire_90": window_sum(cs_n, t, 90), "fire_365": fire_365,
            "frp_30": window_sum(cs_f, t, 30), "days_since_fire": since[t],
            "nb_30": fire_30 @ nb_mat, "nb_365": fire_365 @ nb_mat,
            "label_n": label,
        }
        if W is not None:
            cols.update({
                "gwetroot": W["GWETROOT"][t], "gwettop": W["GWETTOP"][t],
                "precip_30": window_sum(cs_w["PRECTOTCORR"], t, 30), "precip_90": window_sum(cs_w["PRECTOTCORR"], t, 90),
                "tmax_30": window_sum(cs_w["T2M_MAX"], t, 30) / 30, "rh_30": window_sum(cs_w["RH2M"], t, 30) / 30,
                "wind_30": window_sum(cs_w["WS2M_MAX"], t, 30) / 30, "elevation": S["elev"]})
        rows.append(pd.DataFrame(cols))
    X = pd.concat(rows, ignore_index=True)
    X["y"] = np.where(X["label_n"].isna(), np.nan, (X["label_n"] >= LABEL_MIN).astype(float))

    # anomalies vs. the same square + week in the training years ("drier than usual for November?")
    tr = X["date"] <= pd.Timestamp(TRAIN_END)
    for col in ["gwetroot", "precip_90", "tmax_30"] if W is not None else []:
        norm = X[tr].groupby(["cell", "week"])[col].mean().rename("m")
        X = X.join(norm, on=["cell", "week"])
        X[col + "_anom"] = X[col] - X["m"]
        X = X.drop(columns="m")
    return X, now


def add_clim(X, fit_mask):
    """Climatology = share of fit-period years with fire in this square in this week (±1 week).
    Rows inside the fit period leave their own year out, so a row never sees its own answer."""
    F = X[fit_mask & X["y"].notna()]
    g = F.groupby(["cell", "week", "year"])["y"].mean().reset_index()
    parts = [g.assign(week=(g["week"] + k) % 52) for k in (-1, 0, 1)]   # smooth over neighbouring weeks
    g = pd.concat(parts).groupby(["cell", "week", "year"])["y"].mean().reset_index()
    tot = g.groupby(["cell", "week"])["y"].agg(["sum", "count"])
    X = X.drop(columns=["clim"], errors="ignore").join(tot, on=["cell", "week"])
    own = X.join(g.set_index(["cell", "week", "year"])["y"].rename("own"), on=["cell", "week", "year"])["own"]
    loo = fit_mask & own.notna()
    X["clim"] = np.where(loo, (X["sum"] - own) / (X["count"] - 1).clip(lower=1), X["sum"] / X["count"].clip(lower=1))
    X["clim"] = X["clim"].fillna(0)
    return X.drop(columns=["sum", "count"])


def scores(y, p, dates):
    """PR-AUC (main score), ROC-AUC, Brier, and 'how many fires fall in the top 10% riskiest squares'."""
    df = pd.DataFrame({"y": y, "p": p, "d": dates})
    caught = []
    for _, g in df.groupby("d"):
        if g["y"].sum() == 0:
            continue
        top = g.nlargest(max(1, len(g) // 10), "p")
        caught.append(top["y"].sum() / g["y"].sum())
    return {"pr_auc": round(float(average_precision_score(y, p)), 3),
            "roc_auc": round(float(roc_auc_score(y, p)), 3),
            "brier": round(float(brier_score_loss(y, p)), 4),
            "top10pct_captures": round(float(np.mean(caught)), 3),
            "fire_rate": round(float(np.mean(y)), 3)}


def calibration(y, p, bins=(0, .05, .1, .2, .3, .5, .7, 1.01)):
    df = pd.DataFrame({"y": y, "p": p})
    df["b"] = pd.cut(df["p"], bins, right=False)
    t = df.groupby("b", observed=True).agg(predicted=("p", "mean"), actual=("y", "mean"), n=("y", "size"))
    return [{"bin": str(i), "predicted": round(r.predicted, 3), "actual": round(r.actual, 3), "n": int(r.n)}
            for i, r in t.iterrows()]


def gbm():
    return HistGradientBoostingClassifier(max_iter=600, learning_rate=0.04, max_leaf_nodes=31,
                                          min_samples_leaf=80, l2_regularization=1.0,
                                          early_stopping=True, validation_fraction=0.15,
                                          n_iter_no_change=40, random_state=0)


def main(region):
    t0 = time.time()
    S = load(region)
    GROUPS = feature_groups(S["R"])
    FEATURES = [f for fs in GROUPS.values() for f in fs]
    sensor = S["R"].get("sensor", "VIIRS")
    X, now = build(S)
    log(f"table: {len(X):,} rows ({X['t'].nunique()} forecast days x {len(S['cells'])} squares)")

    known = X["y"].notna()
    d = X["date"]
    train = known & (d <= pd.Timestamp(TRAIN_END - timedelta(days=HORIZON)))
    val = known & (d > pd.Timestamp(TRAIN_END)) & (d <= pd.Timestamp(VAL_END - timedelta(days=HORIZON)))
    test = known & (d > pd.Timestamp(VAL_END))
    X = add_clim(X, train)

    res = {}
    res["climatology baseline"] = {"val": scores(X.y[val], X.clim[val], d[val]), "test": scores(X.y[test], X.clim[test], d[test])}
    lr = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000, C=0.5))
    lr.fit(X.loc[train, FEATURES].fillna(0), X.y[train])
    p = lambda m, mask: m.predict_proba(X.loc[mask, FEATURES].fillna(0) if m is lr else X.loc[mask, FEATURES])[:, 1]
    res["logistic regression"] = {"val": scores(X.y[val], p(lr, val), d[val]), "test": scores(X.y[test], p(lr, test), d[test])}
    gb = gbm().fit(X.loc[train, FEATURES], X.y[train])
    res["gradient boosting"] = {"val": scores(X.y[val], p(gb, val), d[val]), "test": scores(X.y[test], p(gb, test), d[test])}
    for k, v in res.items():
        log(f"{k:22s} val {v['val']}\n{'':31s}test {v['test']}")

    # which inputs mattered (on the test years): how much PR-AUC drops when a group is scrambled
    sub = X[test].sample(min(40_000, int(test.sum())), random_state=0)
    pi = permutation_importance(gb, sub[FEATURES], sub["y"], scoring="average_precision", n_repeats=3, random_state=0, n_jobs=-1)
    imp = dict(zip(FEATURES, pi.importances_mean))
    groups = sorted(((g, sum(imp[f] for f in fs)) for g, fs in GROUPS.items()), key=lambda x: -x[1])
    log("what mattered:", [(g, round(v, 3)) for g, v in groups])
    cal = calibration(X.y[test], p(gb, test))

    # final model: retrain on everything with known answers (climatology from all of it, too), predict "now"
    allk = known
    X = add_clim(X, allk)
    final = gbm().fit(X.loc[allk, FEATURES], X.y[allk])
    nowrows = X[X["t"] == now]
    prob = final.predict_proba(nowrows[FEATURES])[:, 1]
    issued = S["day0"] + timedelta(days=now)
    c = S["R"]["cell"]
    out = {
        "region": region, "name": S["R"]["name"], "cell": c,
        "issued": issued.isoformat(),
        "valid_from": (issued + timedelta(days=1)).isoformat(),
        "valid_to": (issued + timedelta(days=HORIZON)).isoformat(),
        "question": f"Chance that satellites detect a fire (at least {LABEL_MIN} {sensor} detections) in this square in the next {HORIZON} days",
        "model": "gradient-boosted trees (scikit-learn HistGradientBoosting)",
        "sensor": sensor, "weather": bool(S["R"].get("weather", True)),
        "trained_on": f"{FIRST_DAY[:4]}-{(issued - timedelta(days=HORIZON)).year}",
        "test": {"years": f"2023-{(issued - timedelta(days=HORIZON)).year}",
                 **{k: v["test"] for k, v in res.items()}},
        "what_mattered": [{"input": g, "importance": round(float(v), 4)} for g, v in groups],
        "calibration_test": cal,
        "fields": ["west", "south", "probability", "usual_for_time_of_year"],
        "cells": [[round(ix * c, 4), round(iy * c, 4), round(float(pr), 4), round(float(cl), 4)]
                  for (ix, iy), pr, cl in zip(S["cells"], prob, nowrows["clim"])],
    }
    os.makedirs(os.path.join(ROOT, "predictions"), exist_ok=True)
    with open(os.path.join(ROOT, "predictions", f"{region}.json"), "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    with open(os.path.join(HERE, "data", region, "report.json"), "w") as fh:
        json.dump({k: v for k, v in out.items() if k != "cells"} | {"validation": {k: v["val"] for k, v in res.items()}}, fh, indent=2)
    idx = os.path.join(ROOT, "predictions", "index.json")          # list of regions the page can show
    regions = json.load(open(idx)) if os.path.exists(idx) else []
    regions = [r for r in regions if r["region"] != region] + [{"region": region, "name": out["name"], "issued": out["issued"]}]
    with open(idx, "w") as fh:
        json.dump(sorted(regions, key=lambda r: r["region"]), fh, indent=1)
    log(f"forecast issued {out['issued']} for {out['valid_from']}..{out['valid_to']}: "
        f"mean p={prob.mean():.3f}, max p={prob.max():.3f}; wrote predictions/{region}.json ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "california")

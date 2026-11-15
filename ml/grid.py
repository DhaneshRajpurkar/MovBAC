"""Grid helpers: which squares belong to a region, and which NASA POWER weather pixel each one uses.

A square is identified by integer indices (ix, iy): it covers [ix*cell, (ix+1)*cell] x [iy*cell, (iy+1)*cell].
"""
import math
import os

import numpy as np

from regions import REGIONS

HERE = os.path.dirname(os.path.abspath(__file__))
POWER_DLAT, POWER_DLON = 0.5, 0.625        # NASA POWER (MERRA-2) grid spacing


def inside(lon, lat, poly):
    """Point-in-polygon (ray casting)."""
    hit = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def bbox_grid(R):
    """Index ranges covering the region's bbox: (ix0, iy0, nx, ny)."""
    c = R["cell"]
    W, S, E, N = R["bbox"]
    ix0, iy0 = math.floor(W / c + 1e-9), math.floor(S / c + 1e-9)
    return ix0, iy0, math.ceil(E / c - 1e-9) - ix0, math.ceil(N / c - 1e-9) - iy0


def fires_path(region):
    return os.path.join(HERE, "data", region, "firms_daily.npz")


def cells(region):
    """List of (ix, iy) squares modelled for a region (inside its outline, and burning often enough)."""
    R = REGIONS[region]
    c = R["cell"]
    ix0, iy0, nx, ny = bbox_grid(R)
    total = None
    if R.get("min_fires"):
        with np.load(fires_path(region)) as z:
            total = z["count"].sum(axis=0)                   # (nx*ny,) detections per square, all days
    out = []
    for i in range(nx):
        for j in range(ny):
            ix, iy = ix0 + i, iy0 + j
            lon, lat = (ix + 0.5) * c, (iy + 0.5) * c
            if R.get("polygons") and not any(inside(lon, lat, p) for p in R["polygons"]):
                continue
            if total is not None and total[i * ny + j] < R["min_fires"]:
                continue
            out.append((ix, iy))
    return out


def cell_centers(region):
    c = REGIONS[region]["cell"]
    return [((ix + 0.5) * c, (iy + 0.5) * c) for ix, iy in cells(region)]


def power_pixel(lon, lat):
    return (round((lon + 180) / POWER_DLON), round((lat + 90) / POWER_DLAT))


def power_center(px):
    return px[0] * POWER_DLON - 180, px[1] * POWER_DLAT - 90

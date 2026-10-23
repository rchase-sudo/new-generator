"""Shared geometry: a local feet frame (rotated so the site fills the sheet), the existing-ground surface on a 5 ft grid,
and grid helpers (polygon masks, steepest-descent tracing)."""
import math

import numpy as np
import shapely
from scipy import ndimage as ndi
from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

CELL = 5.0      # ft


def polys(g):
    if g is None or g.is_empty:
        return []
    if g.geom_type == "Polygon":
        return [g]
    return [p for p in getattr(g, "geoms", []) for p in polys(p)]


def lines(g):
    if g is None or g.is_empty:
        return []
    if g.geom_type == "LineString":
        return [g]
    return [q for p in getattr(g, "geoms", []) for q in lines(p)]


class Frame:
    """State plane (units k ft) <-> local feet, origin at the parcel centroid, rotated by `rot` degrees so the long axis of
    the site runs left-right on the sheet."""

    def __init__(self, parcel_sp, k):
        self.k = k
        c = parcel_sp.centroid
        self.o = (c.x, c.y)
        mrr = parcel_sp.minimum_rotated_rectangle
        cs = list(mrr.exterior.coords)
        e = [(math.dist(cs[i], cs[i + 1]), math.atan2(cs[i + 1][1] - cs[i][1], cs[i + 1][0] - cs[i][0])) for i in range(2)]
        ang = math.degrees(max(e)[1]) % 180
        if ang > 90:
            ang -= 180
        self.rot = -ang          # rotate by this to make the long axis horizontal (kept within +-90)

    def fwd(self, g):
        if g is None:
            return None
        g = affinity.translate(g, -self.o[0], -self.o[1])
        g = affinity.scale(g, self.k, self.k, origin=(0, 0))
        return affinity.rotate(g, self.rot, origin=(0, 0))

    def inv(self, g):
        g = affinity.rotate(g, -self.rot, origin=(0, 0))
        g = affinity.scale(g, 1 / self.k, 1 / self.k, origin=(0, 0))
        return affinity.translate(g, self.o[0], self.o[1])

    def pts_inv(self, x, y):
        a = math.radians(-self.rot)
        xr = x * math.cos(a) - y * math.sin(a)
        yr = x * math.sin(a) + y * math.cos(a)
        return xr / self.k + self.o[0], yr / self.k + self.o[1]

    @property
    def north_deg(self):
        """Direction of north on the sheet, degrees counter-clockwise from +x."""
        return 90.0 + self.rot


class Grid:
    """A regular local grid covering `bounds` (ft). Row 0 is the bottom (min y)."""

    def __init__(self, bounds, cell=CELL):
        x0, y0, x1, y1 = bounds
        self.cell = cell
        self.x0, self.y0 = math.floor(x0 / cell) * cell, math.floor(y0 / cell) * cell
        self.nx, self.ny = int((x1 - self.x0) / cell) + 2, int((y1 - self.y0) / cell) + 2
        self.xs = self.x0 + (np.arange(self.nx) + 0.5) * cell
        self.ys = self.y0 + (np.arange(self.ny) + 0.5) * cell
        self.X, self.Y = np.meshgrid(self.xs, self.ys)

    def mask(self, g):
        if g is None or g.is_empty:
            return np.zeros(self.X.shape, bool)
        return shapely.contains_xy(g, self.X, self.Y)

    def ij(self, x, y):
        return int(np.clip((y - self.y0) / self.cell, 0, self.ny - 1)), int(np.clip((x - self.x0) / self.cell, 0, self.nx - 1))

    def at(self, a, x, y):
        """Bilinear sample of grid array `a` at local points."""
        fi = (np.asarray(x) - self.x0) / self.cell - 0.5
        fj = (np.asarray(y) - self.y0) / self.cell - 0.5
        return ndi.map_coordinates(a, [np.atleast_1d(fj), np.atleast_1d(fi)], order=1, mode="nearest")

    def cell_poly(self, mask):
        """Union of the cells in a mask as a (smoothed) polygon."""
        h = self.cell / 2
        ys, xs = np.nonzero(mask)
        if not len(xs):
            return Polygon()
        cells = [box(self.xs[i] - h, self.ys[j] - h, self.xs[i] + h, self.ys[j] + h) for j, i in zip(ys, xs)]
        return unary_union(cells).buffer(0)


def existing_surface(dem, fr, grid):
    """Existing ground (ft) on the local grid from the 3DEP grid (state plane)."""
    from .. import topo
    x, y = fr.pts_inv(grid.X, grid.Y)
    z = topo.sample(dem, x, y, "zs")
    # bilinear on the DEM grid for smoother contours
    h, w = dem["zs"].shape
    fi = (np.asarray(x) - dem["x0"]) / dem["cell"] - 0.5
    fj = (dem["y1"] - np.asarray(y)) / dem["cell"] - 0.5
    z = ndi.map_coordinates(dem["zs"], [fj, fi], order=1, mode="nearest")
    return ndi.gaussian_filter(z, 1.0)


def slope_pct(z, cell):
    gy, gx = np.gradient(z, cell)
    return 100.0 * np.hypot(gx, gy)


def descend(z, i, j, mask_stop=None, max_steps=4000):
    """Steepest-descent path (D8) from cell (j,i); returns list of (j,i). Stops at a sink, at a stop-mask cell, or the edge."""
    ny, nx = z.shape
    path = [(j, i)]
    seen = {(j, i)}
    for _ in range(max_steps):
        best, bj, bi = 0.0, None, None
        for dj, di in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
            jj, ii = j + dj, i + di
            if 0 <= jj < ny and 0 <= ii < nx and (jj, ii) not in seen:
                d = (z[j, i] - z[jj, ii]) / (1.414 if dj and di else 1.0)
                if d > best:
                    best, bj, bi = d, jj, ii
        if bj is None:
            break
        j, i = bj, bi
        seen.add((j, i))
        path.append((j, i))
        if mask_stop is not None and mask_stop[j, i]:
            break
    return path


def bearing_text(p, q):
    """Surveyor bearing of p->q in the local frame is NOT meaningful (rotated); callers pass state-plane points."""
    dx, dy = q[0] - p[0], q[1] - p[1]
    ang = math.degrees(math.atan2(dx, dy)) % 360       # azimuth from north
    ns = "N" if (ang < 90 or ang > 270) else "S"
    ew = "E" if ang < 180 else "W"
    a = ang if ang <= 90 else (180 - ang if ang <= 180 else (ang - 180 if ang <= 270 else 360 - ang))
    d = int(a)
    m = int((a - d) * 60)
    s = int(round(((a - d) * 60 - m) * 60))
    if s == 60:
        s, m = 0, m + 1
    return f"{ns}{d:02d}°{m:02d}'{s:02d}\"{ew}"

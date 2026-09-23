"""施工总平面：几何与检查。几何同时供 Rhino 画图和检查使用。"""
import math

from . import config as C
from .model import OUT_X, OUT_Y


def slots():
    """架位矩形列表 [(x0, y0, x1, y1)]。"""
    x0, y0 = C.LAYDOWN_ORIGIN
    out = []
    for r in range(C.LAYDOWN_ROWS):
        y = y0 + r * (C.SLOT_W + C.LAYDOWN_AISLE)
        for c in range(C.LAYDOWN_COLS):
            x = x0 + c * C.SLOT_L
            out.append((x, y, x + C.SLOT_L, y + C.SLOT_W))
    return out


def laydown_bbox():
    s = slots()
    return (min(a[0] for a in s), min(a[1] for a in s), max(a[2] for a in s), max(a[3] for a in s))


def building_bbox():
    return (0, 0, OUT_X, OUT_Y)


def _corners(r):
    return [(r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])]


def _inside(r, outer):
    return r[0] >= outer[0] and r[1] >= outer[1] and r[2] <= outer[2] and r[3] <= outer[3]


def _overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def run_checks(peak_stock):
    cx, cy = C.CRANE
    far_b = max(math.hypot(x - cx, y - cy) for x, y in _corners(building_bbox()))
    far_l = max(math.hypot(x - cx, y - cy) for s in slots() for x, y in _corners(s))
    ex = C.EXCLUSION
    b = building_bbox()
    zone = (b[0] - ex, b[1] - ex, b[2] + ex, b[3] + ex)
    ld = laydown_bbox()
    n_slots = len(slots())
    return [
        ("塔吊覆盖整栋楼（半径 %.0f m）" % (C.CRANE_RADIUS / 1000), far_b <= C.CRANE_RADIUS,
         "最远楼角 %.1f m" % (far_b / 1000)),
        ("塔吊覆盖全部架位", far_l <= C.CRANE_RADIUS, "最远架位角 %.1f m" % (far_l / 1000)),
        ("堆场架位 ≥ 排程峰值", n_slots >= peak_stock,
         "布置图 %d 个架位，排程峰值 %d 架" % (n_slots, peak_stock)),
        ("堆场不进坠物禁区（楼外 %.0f m）" % (ex / 1000), not _overlap(ld, zone),
         "堆场北边 y=%.1f m，禁区南边 y=%.1f m" % (ld[3] / 1000, zone[1] / 1000)),
        ("堆场、塔吊、车道都在红线内", _inside(ld, C.SITE) and all(
            C.SITE[0] <= x <= C.SITE[2] and C.SITE[1] <= y <= C.SITE[3] for x, y in C.TRUCK_ROUTE + [C.CRANE]),
         "红线 %.1f×%.1f m" % ((C.SITE[2] - C.SITE[0]) / 1000, (C.SITE[3] - C.SITE[1]) / 1000)),
    ]

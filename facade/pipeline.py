"""把模型、检查、排程、工程量算成一组表。Rhino 脚本、IFC 导出、测试都调这里。"""
from collections import OrderedDict, defaultdict

from . import config as C, schedule as S, site
from .checks import run_all
from .model import TYPE_NAMES, build_panels

QTY_KEYS = ["vision_igu_m2", "spandrel_m2", "louver_m2", "door_glass_m2",
            "frame_alu_kg", "backpan_alu_kg", "coping_alu_kg", "insulation_m2"]


def compute(trucks_per_day=None, buffer_days=None):
    panels = build_panels()
    install = S.install_plan(panels)
    stillages, trucks = S.delivery_plan(panels, install, buffer_days=buffer_days, trucks_per_day=trucks_per_day)
    stock = S.site_stock(stillages)
    summary = S.summarize(panels, install, stillages, trucks, stock)
    where = {}
    for s in stillages:
        for pid in s["panels"]:
            where[pid] = s
    return {"panels": panels, "install": install, "stillages": stillages, "trucks": trucks,
            "stock": stock, "summary": summary, "where": where}


def panel_rows(r):
    rows = []
    for p in r["panels"]:
        seq, day = r["install"][p.pid]
        s = r["where"][p.pid]
        row = OrderedDict([
            ("pid", p.pid), ("elev", p.elev), ("level", p.level), ("col", p.col),
            ("type", p.ptype), ("type_name", TYPE_NAMES[p.ptype]),
            ("w_mm", p.w), ("h_mm", p.h),
            ("origin_x", "%.1f" % p.origin[0]), ("origin_y", "%.1f" % p.origin[1]),
            ("origin_z", "%.1f" % p.origin[2]), ("rot", p.rot),
        ])
        for k in QTY_KEYS:
            row[k] = "%.3f" % p.qty[k]
        row.update([("weight_kg", "%.1f" % p.weight_kg), ("seq", seq), ("install_date", day.isoformat()),
                    ("stillage", s["stillage"]), ("truck", s["truck"]), ("delivery_date", s["delivered"].isoformat())])
        rows.append(row)
    return rows


def takeoff_rows(panels):
    agg = defaultdict(lambda: defaultdict(float))
    for p in panels:
        for key in (p.ptype, "合计"):
            a = agg[key]
            a["count"] += 1
            a["facade_m2"] += C.MODULE * (p.h + C.JOINT) / 1e6
            for k in QTY_KEYS:
                a[k] += p.qty[k]
            a["brackets"] += p.qty["brackets"]
            a["weight_kg"] += p.weight_kg
    rows = []
    for key in sorted(k for k in agg if k != "合计") + ["合计"]:
        a = agg[key]
        row = OrderedDict([("type", key), ("type_name", TYPE_NAMES.get(key, "全部")), ("count", int(a["count"])),
                           ("facade_m2", "%.2f" % a["facade_m2"])])
        for k in QTY_KEYS:
            row[k] = "%.2f" % a[k]
        row["brackets"] = int(a["brackets"])
        row["weight_t"] = "%.2f" % (a["weight_kg"] / 1000.0)
        rows.append(row)
    return rows


def sensitivity_rows():
    rows = []
    for tpd in (1, 2, 3):
        for b in (1, 2, 3):
            r = compute(trucks_per_day=tpd, buffer_days=b)
            sm = r["summary"]
            rows.append(OrderedDict([
                ("trucks_per_day", tpd), ("buffer_days", b), ("peak_stock", sm["peak_stock"]),
                ("stillages_early", sm["stillages_pulled_early"]),
                ("first_delivery", sm["first_delivery"].isoformat()),
                ("fits_laydown", "yes" if sm["peak_stock"] <= len(site.slots()) else "no")]))
    return rows


def all_checks(r):
    out = []
    for name, ok, msg in run_all(r["panels"]):
        out.append({"group": "模型", "name": name, "pass": bool(ok), "detail": msg})
    for name, ok, msg in site.run_checks(r["summary"]["peak_stock"]):
        out.append({"group": "工地布置", "name": name, "pass": bool(ok), "detail": msg})
    return out

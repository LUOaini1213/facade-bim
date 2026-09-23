"""4D 安装排程、运输架装箱、到场计划与堆场占用。

- 安装：按板块生成顺序逐块排入工作日，每日工时上限 = 班组数 × 每班工时；
  一块板不跨日拆分。
- 装架：沿安装顺序，把连续的同类型板块按每架容量装入运输架。
- 到场：每个架子最迟在其首块板安装日前 BUFFER_DAYS 个工作日到场；
  连续的架子按每车容量拼车，一车的到场日取车上最急那个架子的最迟日；
  每日卸车数超上限时，从后往前把多出来的车挪到前一个工作日（倒排，尽量准时）。
- 堆场：某日的在场架数 = 当日及以前已到场 − 当日以前已装空（装空的架子当日运走）。
"""
from collections import OrderedDict, defaultdict
from datetime import timedelta

from . import config as C


def is_workday(d):
    return d.weekday() in C.WORKDAYS


def add_workdays(d, n):
    step = 1 if n >= 0 else -1
    while n:
        d += timedelta(days=step)
        if is_workday(d):
            n -= step
    return d


def install_plan(panels, crews=None, crew_hours=None):
    """返回 {pid: (序号, 安装日)}。"""
    cap = (crews or C.CREWS) * (crew_hours or C.CREW_HOURS)
    day = C.INSTALL_START
    while not is_workday(day):
        day += timedelta(days=1)
    used, out = 0.0, OrderedDict()
    for seq, p in enumerate(panels, 1):
        t = C.INSTALL_HOURS[p.ptype]
        if used + t > cap + 1e-9:
            day, used = add_workdays(day, 1), 0.0
        out[p.pid] = (seq, day)
        used += t
    return out


def pack_stillages(panels):
    """返回 [(架号, 板型, [pid...])]，沿安装顺序连续装。"""
    stillages, cur, cur_type = [], [], None
    for p in panels:
        if cur and (p.ptype != cur_type or len(cur) == C.STILLAGE_CAP[cur_type]):
            stillages.append((cur_type, cur))
            cur = []
        cur_type = p.ptype
        cur.append(p.pid)
    if cur:
        stillages.append((cur_type, cur))
    return [("ST-%03d" % i, t, ids) for i, (t, ids) in enumerate(stillages, 1)]


def delivery_plan(panels, install, buffer_days=None, trucks_per_day=None, truck_stillages=None):
    """返回 (架子表, 车次表)。架子表每项含首/末安装日、最迟到场日、实际到场日、车号。"""
    buffer_days = C.BUFFER_DAYS if buffer_days is None else buffer_days
    trucks_per_day = trucks_per_day or C.TRUCKS_PER_DAY
    truck_stillages = truck_stillages or C.TRUCK_STILLAGES
    st = []
    for sid, ptype, ids in pack_stillages(panels):
        days = [install[i][1] for i in ids]
        first, last = min(days), max(days)
        st.append({"stillage": sid, "type": ptype, "panels": ids, "n": len(ids),
                   "first_install": first, "last_install": last,
                   "due": add_workdays(first, -buffer_days)})
    trucks = []
    for k in range(0, len(st), truck_stillages):
        group = st[k:k + truck_stillages]
        trucks.append({"truck": "TR-%03d" % (len(trucks) + 1),
                       "stillages": [s["stillage"] for s in group],
                       "date": min(s["due"] for s in group)})
    # 倒排：从最晚的车开始，一天超过卸车上限就往前挪
    load = defaultdict(int)
    for t in sorted(trucks, key=lambda t: (t["date"], t["truck"]), reverse=True):
        d = t["date"]
        while load[d] >= trucks_per_day or not is_workday(d):
            d = add_workdays(d, -1) if is_workday(d) else d - timedelta(days=1)
        t["date"] = d
        load[d] += 1
    by_sid = {s["stillage"]: s for s in st}
    for t in trucks:
        for sid in t["stillages"]:
            by_sid[sid]["delivered"] = t["date"]
            by_sid[sid]["truck"] = t["truck"]
    return st, trucks


def site_stock(stillages):
    """返回 [(日期, 在场架数)]，覆盖首车到场到末块板装完的每个工作日。"""
    start = min(s["delivered"] for s in stillages)
    end = max(s["last_install"] for s in stillages)
    out, d = [], start
    while d <= end:
        if is_workday(d):
            n = sum(1 for s in stillages if s["delivered"] <= d and s["last_install"] >= d)
            out.append((d, n))
        d += timedelta(days=1)
    return out


def summarize(panels, install, stillages, trucks, stock):
    days = sorted(set(d for _, d in install.values()))
    peak_day, peak = max(stock, key=lambda x: (x[1], x[0]))
    early = sum(1 for s in stillages if s["delivered"] < s["due"])
    return {
        "install_first": days[0], "install_last": days[-1], "install_workdays": len(days),
        "stillages": len(stillages), "trucks": len(trucks),
        "first_delivery": min(t["date"] for t in trucks),
        "peak_stock": peak, "peak_day": peak_day,
        "stillages_pulled_early": early,
        "late": sum(1 for s in stillages if s["delivered"] > s["due"]),
    }

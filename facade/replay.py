"""施工日期状态；只用模型中的安装/到场属性，不另算一份排程。

状态取当日施工期间：当日安装板已显示，运输架在装空当日仍占位。
与 schedule.site_stock 的含首尾日期口径一致。架位为示意分配，不是装架几何。
"""
from collections import Counter, defaultdict
from datetime import date


def as_date(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def prepare(rows):
    panels, groups = {}, defaultdict(list)
    for row in rows:
        pid = row["pid"]
        if not pid or pid in panels:
            raise ValueError("板块编号为空或重复：%s" % pid)
        delivery, install = as_date(row["delivery_date"]), as_date(row["install_date"])
        if delivery > install:
            raise ValueError("板块到场晚于安装：%s" % pid)
        sid = row["stillage"]
        if not sid:
            raise ValueError("缺运输架编号：%s" % pid)
        panels[pid] = dict(row, delivery=delivery, install=install)
        groups[sid].append(pid)
    if not panels:
        raise ValueError("模型没有含排程属性的幕墙板块")
    stillages = {}
    for sid, pids in groups.items():
        deliveries = {panels[pid]["delivery"] for pid in pids}
        if len(deliveries) != 1:
            raise ValueError("同一架的到场日不一致：%s" % sid)
        stillages[sid] = {"panels": sorted(pids), "delivery": deliveries.pop(),
                          "last_install": max(panels[pid]["install"] for pid in pids)}
    return panels, stillages


def allocate_slots(stillages, capacity):
    """按到场顺序分配首个空架位；当天装空的架位次日才可复用。"""
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
        raise ValueError("架位数量必须是正整数")
    busy, assigned = {}, {}
    for sid, s in sorted(stillages.items(), key=lambda pair: (pair[1]["delivery"], pair[0])):
        busy = {slot: last for slot, last in busy.items() if last >= s["delivery"]}
        free = next((i for i in range(capacity) if i not in busy), None)
        if free is None:
            raise ValueError("%s 到场时堆场超过 %d 个架位（%s）" % (sid, capacity, s["delivery"]))
        busy[free] = s["last_install"]
        assigned[sid] = free
    return assigned


def snapshot(rows, day, capacity=12):
    day = as_date(day)
    panels, stillages = prepare(rows)
    slots = allocate_slots(stillages, capacity)
    states = {}
    for pid, p in panels.items():
        if p["install"] < day:
            state = "installed"
        elif p["install"] == day:
            state = "installing"
        elif p["delivery"] <= day:
            state = "on_site"
        else:
            state = "not_delivered"
        states[pid] = state
    counts = {key: 0 for key in ("not_delivered", "on_site", "installing", "installed")}
    counts.update(Counter(states.values()))
    active = []
    for sid, s in sorted(stillages.items()):
        if s["delivery"] <= day <= s["last_install"]:
            active.append({"stillage": sid, "slot": slots[sid],
                           "remaining": sum(panels[pid]["install"] >= day for pid in s["panels"])})
    return {"date": day.isoformat(), "counts": counts, "total": len(panels), "states": states,
            "visible_panels": counts["installed"] + counts["installing"],
            "active_stillages": active, "occupied_slots": len(active), "slot_capacity": capacity,
            "semantics": "during_install_day; empty stillages leave after that day"}

"""模型检查：每条规则返回 (名称, 是否通过, 说明)。

规则直接在生成出的几何上算，不信任「按构造应当成立」——
比如接缝是逐对相邻板块量出来的，不是拿模数减接缝推出来的。
"""
from collections import defaultdict

from . import config as C
from .model import OUT_X, OUT_Y, corner_posts, footprint, to_world


def _overlap(a, b, tol=1e-6):
    return a[0] < b[2] - tol and b[0] < a[2] - tol and a[1] < b[3] - tol and b[1] < a[3] - tol


def check_unique_ids(panels):
    ids = [p.pid for p in panels]
    dup = len(ids) - len(set(ids))
    return ("板块编号唯一", dup == 0, "%d 个编号，重复 %d" % (len(ids), dup))


def check_joints(panels):
    """同层同立面相邻板块之间的缝宽必须恰好等于 JOINT；两端到转角立柱也是半条缝。"""
    runs = defaultdict(list)
    for p in panels:
        runs[(p.level, p.elev)].append(p)
    bad, n = [], 0
    for (level, elev), ps in runs.items():
        ps.sort(key=lambda q: q.col)
        # 沿立面方向的一维区间：局部 x 从 0 到 w 映射到世界坐标
        spans = []
        for p in ps:
            a = to_world(p.origin, p.rot, 0, 0, 0)
            b = to_world(p.origin, p.rot, p.w, 0, 0)
            axis = 0 if elev in ("S", "N") else 1
            lo, hi = sorted((a[axis], b[axis]))
            spans.append((lo, hi))
        spans.sort()
        for (lo1, hi1), (lo2, hi2) in zip(spans, spans[1:]):
            n += 1
            gap = lo2 - hi1
            if abs(gap - C.JOINT) > 1e-6:
                bad.append((level, elev, round(gap, 3)))
        run_len = (C.N_LONG if elev in ("S", "N") else C.N_SHORT) * C.MODULE
        edge_lo, edge_hi = C.CORNER, C.CORNER + run_len
        for gap in (spans[0][0] - edge_lo, edge_hi - spans[-1][1]):
            n += 1
            if abs(gap - C.JOINT / 2.0) > 1e-6:
                bad.append((level, elev, "端部", round(gap, 3)))
    return ("竖缝宽度 = %d mm（逐对量）" % C.JOINT, not bad, "量了 %d 处，偏差 %d 处 %s" % (n, len(bad), bad[:3]))


def check_stack_joints(panels):
    """上下层板块之间的横缝宽度。"""
    by_pos = defaultdict(dict)
    for p in panels:
        by_pos[(p.elev, p.col)][p.level] = p
    order = [lv for lv, _, _ in C.LEVELS]
    bad, n = [], 0
    for (elev, col), lv in by_pos.items():
        for a, b in zip(order, order[1:]):
            if a in lv and b in lv:
                n += 1
                gap = lv[b].origin[2] - (lv[a].origin[2] + lv[a].h)
                if abs(gap - C.JOINT) > 1e-6:
                    bad.append((elev, col, a, b, gap))
    return ("横缝宽度 = %d mm（逐对量）" % C.JOINT, not bad, "量了 %d 处，偏差 %d 处" % (n, len(bad)))


def check_no_clash(panels):
    """同层内任意两块板、以及板与转角立柱，平面包络不相交。"""
    by_level = defaultdict(list)
    for p in panels:
        by_level[p.level].append(footprint(p))
    posts = [(x, y, x + s, y + s) for x, y, s, _ in corner_posts()]
    pairs = clashes = 0
    for fps in by_level.values():
        for i in range(len(fps)):
            for j in range(i + 1, len(fps)):
                pairs += 1
                clashes += _overlap(fps[i], fps[j])
            for post in posts:
                pairs += 1
                clashes += _overlap(fps[i], post)
    return ("板块与板块、板块与转角立柱无冲突", clashes == 0, "逐对检查 %d 对，冲突 %d" % (pairs, clashes))


def check_envelope(panels):
    """所有板块落在外轮廓之内，外表面贴齐外轮廓线。"""
    bad = 0
    for p in panels:
        x0, y0, x1, y1 = footprint(p)
        if x0 < -1e-6 or y0 < -1e-6 or x1 > OUT_X + 1e-6 or y1 > OUT_Y + 1e-6:
            bad += 1
    return ("板块位于外轮廓 %d×%d mm 内" % (OUT_X, OUT_Y), bad == 0, "越界 %d 块" % bad)


def check_glass_size(panels):
    """单片玻璃（净宽 × 分区高）不超过加工上限。"""
    worst, worst_pid = 0.0, None
    over = 0
    for p in panels:
        net_w = p.w - 2 * C.MULLION_W
        for kind, zh in p.zones.items():
            if kind.startswith(("vision", "spandrel", "door")):
                util = max(net_w / C.GLASS_MAX_W, zh / C.GLASS_MAX_H)
                if util > worst:
                    worst, worst_pid = util, "%s %s %d×%d" % (p.pid, kind, net_w, zh)
                over += util > 1
    return ("单片玻璃 ≤ %d×%d mm" % (C.GLASS_MAX_W, C.GLASS_MAX_H), over == 0,
            "超限 %d 片；最接近上限：%s，利用率 %.0f%%" % (over, worst_pid, worst * 100))


def check_lift(panels):
    heaviest = max(panels, key=lambda p: p.weight_kg)
    over = sum(p.weight_kg > C.LIFT_SWL_KG for p in panels)
    return ("吊装重 ≤ 小吊机额定 %.0f kg" % C.LIFT_SWL_KG, over == 0,
            "超限 %d 块；最重 %s %.1f kg，利用率 %.0f%%" % (
                over, heaviest.pid, heaviest.weight_kg, heaviest.weight_kg / C.LIFT_SWL_KG * 100))


ALL = [check_unique_ids, check_joints, check_stack_joints, check_no_clash,
       check_envelope, check_glass_size, check_lift]


def run_all(panels):
    return [f(panels) for f in ALL]

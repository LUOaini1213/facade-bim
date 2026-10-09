"""README 里的每个数字，对着已提交的产物回算。

每条核对 = 一条在 README 里必须恰好命中一次的正则 + 一个从产物算出期望值的函数。
最后断言核对条数等于 EXPECTED——改 README 时某句不再匹配，不会被静默跳过。

    python scripts/check_readme.py
"""
import csv
import hashlib
import json
import logging
import os
import re
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ifcopenshell            # noqa: E402
import ifcopenshell.validate   # noqa: E402
import rhino3dm                # noqa: E402

from facade import config as C, site          # noqa: E402
from facade.model import TYPE_NAMES, build_panels  # noqa: E402

EXPECTED = 73
README = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()


def rows(name):
    return list(csv.DictReader(open(os.path.join(ROOT, "data", name), encoding="utf-8")))


SUMMARY = json.load(open(os.path.join(ROOT, "data", "summary.json"), encoding="utf-8"))
CHECKS = json.load(open(os.path.join(ROOT, "data", "checks.json"), encoding="utf-8"))
PANELS = rows("panels.csv")
TAKEOFF = rows("takeoff.csv")
SENS = rows("sensitivity.csv")
M3 = rhino3dm.File3dm.Read(os.path.join(ROOT, "model", "facade_bim.3dm"))
IFC = ifcopenshell.open(os.path.join(ROOT, "model", "facade_bim.ifc"))
TESTS_MODEL = open(os.path.join(ROOT, "tests", "test_model.py"), encoding="utf-8").read()

failures, checked = [], [0]


def n(s):
    """去掉千分位逗号，便于和产物里的原始数比。"""
    return s.replace(",", "")


def claim(label, pattern, expected):
    """README 里 pattern 必须恰好命中一次，捕获组逐个等于 expected。"""
    checked[0] += 1
    hits = list(re.finditer(pattern, README))
    if len(hits) != 1:
        failures.append("%s：正则命中 %d 次（应为 1）：%s" % (label, len(hits), pattern))
        return
    got = tuple(n(g) for g in hits[0].groups())
    want = tuple(str(x) for x in expected)
    if got != want:
        failures.append("%s：README 写 %s，产物算出 %s" % (label, got, want))


def validate_issues():
    issues = []

    class H(logging.Handler):
        def emit(self, rec):
            issues.append(rec)
    lg = logging.getLogger("readme-ifc-validate")
    lg.addHandler(H())
    lg.setLevel(logging.DEBUG)
    ifcopenshell.validate.validate(IFC, lg, express_rules=False)
    return len(issues)


def main():
    panels = build_panels()
    by_type = {}
    for p in panels:
        by_type.setdefault(p.ptype, []).append(p)
    idefs = [d.Name for d in M3.InstanceDefinitions]
    inst = [o for o in M3.Objects if isinstance(o.Geometry, rhino3dm.InstanceReference) and o.Attributes.GetUserString("pid")]
    n_attrs = len(PANELS[0])
    groups = {}
    for c in CHECKS:
        groups[c["group"]] = groups.get(c["group"], 0) + 1
    passed = sum(c["pass"] for c in CHECKS)
    top = C.LEVELS[-1][1] + C.LEVELS[-1][2]
    perim_m = 2 * (C.N_LONG + C.N_SHORT) * C.MODULE / 1000.0
    total = next(r for r in TAKEOFF if r["type"] == "合计")
    week = lambda r: (date.fromisoformat(r["install_date"]) - C.INSTALL_START).days // 7
    w1 = [r for r in PANELS if week(r) == 0]
    sens = {(int(r["trucks_per_day"]), int(r["buffer_days"])): r for r in SENS}
    ifc_count = lambda c: len(IFC.by_type(c))

    # ---- 英文摘要
    claim("英文摘要：板块数", r"(\d+) panels on a fictional", [len(PANELS)])
    claim("英文摘要：层数", r"fictional\s*\n?(\d+)-storey", [len([lv for lv in C.LEVELS if lv[0] != "RF"])])
    claim("英文摘要：属性数", r"carries (\d+)\s*\n?attributes", [n_attrs])
    # ---- 一眼看懂
    claim("总览：板块/类型/属性", r"\*\*(\d+)\*\* 块单元板块、\*\*(\d+)\*\* 种类型（Rhino 块定义）、每块板挂 \*\*(\d+)\*\* 项属性",
          [len(inst), len(idefs), n_attrs])
    claim("总览：检查条数与通过数", r"模型检查 (\d+) 条 \+ 工地布置检查 (\d+) 条，\*\*(\d+)/(\d+)\*\* 通过",
          [groups["模型"], groups["工地布置"], passed, len(CHECKS)])
    claim("总览：4D 与物流", r"\*\*([\d-]+)\*\* 开装，\*\*(\d+)\*\* 个工作日装完；\*\*(\d+)\*\* 个运输架、\*\*(\d+)\*\* 车；堆场峰值 \*\*(\d+)\*\* 架",
          [SUMMARY["install_first"], SUMMARY["install_workdays"], SUMMARY["stillages"], SUMMARY["trucks"], SUMMARY["peak_stock"]])
    claim("总览：IFC", r"IFC4：\*\*(\d+)\*\* 个 IfcPlate，schema 校验 \*\*(\d+)\*\* 问题", [ifc_count("IfcPlate"), validate_issues()])
    # ---- 模型
    for t in sorted(by_type):
        ps = by_type[t]
        claim("类型表 %s" % t, r"\| %s \| (\S+) \| (\d+) \| (\d+) × (\d+) \| ([\d.]+) \|" % t,
              [TYPE_NAMES[t], len(ps), ps[0].w, ps[0].h, "%.1f" % ps[0].weight_kg])
    from facade.model import OUT_X, OUT_Y
    claim("外轮廓与分格", r"外轮廓 ([\d,]+) × ([\d,]+) mm，南北立面各 (\d+) 列、东西立面各 (\d+) 列，模数 ([\d,]+) mm、接缝 (\d+) mm",
          [OUT_X, OUT_Y, C.N_LONG, C.N_SHORT, C.MODULE, C.JOINT])
    claim("层高", r"L1 层高 ([\d,]+)、L2–L8 层高 ([\d,]+)、屋面女儿墙 ([\d,]+)", [C.LEVELS[0][2], C.LEVELS[1][2], C.LEVELS[-1][2]])
    claim("Rhino 文件对象与图层", r"整个文件 \*\*([\d,]+)\*\* 个对象、\*\*(\d+)\*\* 个图层", [len(M3.Objects), len(M3.Layers)])
    # Read the actual shared coping and cardinal instance transforms, rather
    # than trusting the parameter that claims to have changed it.
    from scripts.check_quality import box_gap, source_box_bounds
    source_objects = {obj.Attributes.Id: obj for obj in M3.Objects}
    source_definitions = {definition.Id: definition for definition in M3.InstanceDefinitions}
    roof = {obj.Attributes.GetUserString("pid"): obj for obj in inst if obj.Attributes.GetUserString("type") == "U5"}
    roof_panel = next(iter(roof.values()))
    cap = source_objects[list(source_definitions[roof_panel.Geometry.ParentIdefId].GetObjectIds())[-1]]
    cap_bounds = source_box_bounds(cap.Geometry)
    assert cap_bounds is not None, "source coping is not a certified box"
    length = cap_bounds[1][0] - cap_bounds[0][0]
    width = cap_bounds[1][1] - cap_bounds[0][1]
    cap_world = {pid: source_box_bounds(cap.Geometry, panel.Geometry.Xform) for pid, panel in roof.items()}
    post = next(obj for obj in M3.Objects if isinstance(obj.Geometry, rhino3dm.Brep)
                and M3.Layers.FindIndex(obj.Attributes.LayerIndex).FullPath == "幕墙::转角立柱"
                and abs(obj.Geometry.GetBoundingBox().Min.X) < 1e-7 and abs(obj.Geometry.GetBoundingBox().Min.Y) < 1e-7)
    post_gap = box_gap(cap_world["S-RF-01"], source_box_bounds(post.Geometry))
    adjacent_gap = box_gap(cap_world["S-RF-01"], cap_world["S-RF-02"])
    corner_gap = box_gap(cap_world["S-RF-01"], cap_world["W-RF-15"])
    mass = length * width * C.COPING_MM * C.ALU_DENSITY / 1e9
    claim("压顶宽度/背边/出挑", r"U5 压顶总宽 (\d+) mm，背边对齐 (\d+) mm 框深，外侧出挑 (\d+) mm",
          ["%g" % width, "%g" % cap_bounds[1][1], "%g" % -cap_bounds[0][1]])
    claim("压顶端缝/退让/净长", r"默认 \*\*(\d+) mm\*\*：从分格中心两端各退让 \*\*(\d+) mm\*\*，制造净长 \*\*(\d+) mm\*\*",
          ["%g" % adjacent_gap, "%g" % post_gap, "%g" % length])
    claim("压顶真实三类净距", r"相邻压顶净距 \*\*([\d.]+) mm\*\*、压顶到角柱 \*\*([\d.]+) mm\*\*、四角两压顶约 \*\*([\d.]+) mm\*\*",
          ["%g" % adjacent_gap, "%g" % post_gap, "%.6f" % corner_gap])
    claim("真实铝板制作尺寸", r"真实铝板用量按 (\d+) × (\d+) × (\d+) mm", ["%g" % length, "%g" % width, "%g" % C.COPING_MM])
    claim("真实压顶质量", r"\*\*([\d.]+) kg/块、([\d.]+) kg/(\d+)块\*\*", ["%.4f" % mass, "%.3f" % (mass * len(roof)), len(roof)])
    old_w = float(roof_panel.Attributes.GetUserString("w_mm"))
    claim("纠正压顶用量差", r"90块压顶铝总量减少 \*\*([\d.]+) kg\*\*", ["%.3f" % ((old_w - length) * width * C.COPING_MM * C.ALU_DENSITY / 1e9 * len(roof))])
    native = json.load(open(os.path.join(ROOT, "model", "quality", "native_all.json"), encoding="utf-8"))
    assert native["source_sha256"] == hashlib.sha256(open(os.path.join(ROOT, "model", "facade_bim.3dm"), "rb").read()).hexdigest(), "stale native quality report"
    q = native["spatial"]
    assert q["clearance_mm"] == 10 and q["clearance_ok"] is True
    claim("完整源模型当前净距结果", r"\*\*(\d+) 条体积穿透、(\d+) 条净距不足、(\d+) 条接触、(\d+) 条距离待核、(\d+) 条内核未决\*\*",
          [len(q["clashes"]), q["clearance_counts"]["below_clearance"], q["clearance_counts"]["contact"], q["clearance_counts"]["threshold_candidate"], len(q["unresolved"])])
    roof_boundaries = sum("-RF-" in event["a"] or "-RF-" in event["b"] for event in q["clearance_events"])
    claim("保留阈值边界", r"\*\*(\d+) 条10 mm阈值边界\*\*（L1–L8端框与角柱(\d+)条、屋顶端框与角柱(\d+)条）",
          [q["clearance_counts"]["at_clearance"], len(q["clearance_events"]) - roof_boundaries, roof_boundaries])
    # ---- 检查表：12 行逐行
    table = [c for c in CHECKS]
    for c in table:
        claim("检查表：%s" % c["name"], r"\| %s \| (✅|❌) \| ([^\n|]+) \|" % re.escape(c["name"]),
              ["✅" if c["pass"] else "❌", c["detail"]])
    claim("反例：挪 5 mm / 重叠 40 mm / 臂长 45 m", r"把一块板挪 (\d+) mm，竖缝或横缝检查必须变红；\s*\n?让两块板重叠 (\d+) mm，冲突检查必须变红；把塔吊臂长改成 (\d+) m",
          [re.search(r'_move\("S-L4-12", dx=(\d+)\)', TESTS_MODEL).group(1),
           str(-int(re.search(r'_move\("N-L2-07", dx=(-\d+)\)', TESTS_MODEL).group(1))),
           str(int(re.search(r"C\.CRANE_RADIUS = (\d+)", TESTS_MODEL).group(1)) // 1000)])
    # ---- 工程量表：6 行逐格
    cols = ["count", "facade_m2", "vision_igu_m2", "spandrel_m2", "louver_m2", "door_glass_m2",
            "frame_alu_kg", "backpan_alu_kg", "coping_alu_kg", "brackets", "weight_t"]
    for r in TAKEOFF:
        claim("工程量表 %s" % r["type"], r"\| %s \| " % r["type"] + r" \| ".join([r"([\d.]+)"] * len(cols)) + r" \|",
              [r[c] for c in cols])
    claim("立面面积对账", r"即 (\d+) m × ([\d.]+) m = ([\d,]+) m²",
          ["%g" % perim_m, "%g" % (top / 1000.0), "%g" % float(total["facade_m2"])])
    # ---- 4D
    claim("班组与工时", r"(两)个班组、每班每日 (\d+) 小时", ["两" if C.CREWS == 2 else str(C.CREWS), "%g" % C.CREW_HOURS])
    claim("安装起止与工作日", r"\*\*([\d-]+)\*\* 开装，\*\*([\d-]+)\*\* 装完，共 \*\*(\d+)\*\* 个工作日",
          [SUMMARY["install_first"], SUMMARY["install_last"], SUMMARY["install_workdays"]])
    claim("第 1 周", r"第 1 周装了 \*\*(\d+)\*\* 块、止于 (\S+-L\d-\d+)，首层西立面整面落进第 (\d) 周",
          [len(w1), w1[-1]["pid"], sorted(set(week(r) + 1 for r in PANELS if r["level"] == "L1" and r["elev"] == "W")) == [2] and 2])
    # ---- 物流
    claim("每架板数与架子数", r"每架 (\d+)–(\d+) 块，按类型），共 \*\*(\d+)\*\* 个架子",
          [min(C.STILLAGE_CAP.values()), max(C.STILLAGE_CAP.values()), SUMMARY["stillages"]])
    claim("到场规则与车数", r"首块板安装前 (\d+) 个工作日到场，连续 (\d+) 个架子拼一车，共 \*\*(\d+)\*\* 车，\s*\n?每日卸车超过 (\d+) 车",
          [C.BUFFER_DAYS, C.TRUCK_STILLAGES, SUMMARY["trucks"], C.TRUCKS_PER_DAY])
    claim("首车与峰值", r"首车 \*\*([\d-]+)\*\* 到场，堆场峰值 \*\*(\d+)\*\* 架（\*\*([\d-]+)\*\*）",
          [SUMMARY["first_delivery"], SUMMARY["peak_stock"], SUMMARY["peak_day"]])
    claim("无迟到、拼车早到", r"(没有)一个架子晚于最迟到场日；拼车让 \*\*(\d+)\*\* 个架子比最迟日早到",
          ["没有" if SUMMARY["late"] == 0 else "有", SUMMARY["stillages_pulled_early"]])
    claim("堆场架位", r"堆场按 (\d+) 排 × (\d+) 列画了 \*\*(\d+)\*\* 个架位", [C.LAYDOWN_ROWS, C.LAYDOWN_COLS, len(site.slots())])
    claim("敏感性表头", r"\| 装得进 (\d+) 个架位 \|", [len(site.slots())])
    for (tpd, b), r in sorted(sens.items()):
        claim("敏感性 %d车/%d天" % (tpd, b), r"\| %d \| %d \| (\d+) \| (\d+) \| ([\d-]+) \| (是|否) \|" % (tpd, b),
              [r["peak_stock"], r["stillages_early"], r["first_delivery"], "是" if r["fits_laydown"] == "yes" else "否"])
    doubled = int(sens[(1, 1)]["peak_stock"]) >= 2 * int(sens[(2, 1)]["peak_stock"])
    same3 = all({k: v for k, v in sens[(3, b)].items() if k != "trucks_per_day"} ==
                {k: v for k, v in sens[(2, b)].items() if k != "trucks_per_day"} for b in (1, 2, 3))
    claim("读法：1 车翻倍、3 车不变", r"堆场峰值(翻倍)；\s*\n?而卸车上限放到 3 车(没有任何变化)",
          ["翻倍" if doubled else "未翻倍", "没有任何变化" if same3 else "有变化"])
    claim("读法：提前 2 天峰值", r"12 个架位只容得下「提前 (\d+) 个工作日到场」，提前 (\d+) 天峰值就到 (\d+) 架",
          [max(b for (t, b), r in sens.items() if t == C.TRUCKS_PER_DAY and r["fits_laydown"] == "yes"),
           2, sens[(C.TRUCKS_PER_DAY, 2)]["peak_stock"]])
    # ---- IFC
    claim("IFC 楼层", r"→ \*\*(\d+)\*\* 个 IfcBuildingStorey", [ifc_count("IfcBuildingStorey")])
    claim("IFC 幕墙段与板", r"IfcCurtainWall，共 \*\*(\d+)\*\* 个，聚合该段的 IfcPlate，共 \*\*(\d+)\*\* 个",
          [ifc_count("IfcCurtainWall"), ifc_count("IfcPlate")])
    claim("IFC 类型", r"\*\*(\d+)\*\* 个 IfcPlateType 对应 Rhino 的 (\d+) 个块定义", [ifc_count("IfcPlateType"), len(idefs)])
    claim("IFC 立柱与楼板", r"\*\*(\d+)\*\* 根转角立柱为 IfcMember，\*\*(\d+)\*\* 块楼板为 IfcSlab", [ifc_count("IfcMember"), ifc_count("IfcSlab")])
    claim("IFC 真实分件", r"\*\*([\d,]+)\*\* 个 IfcBuildingElementPart", [ifc_count("IfcBuildingElementPart")])
    claim("IFC 材料", r"\*\*(\d+)\*\* 种 IfcMaterial", [ifc_count("IfcMaterial")])
    tasks = IFC.by_type("IfcTask")
    claim("IFC 正式任务", r"\*\*([\d,]+)\*\* 个 IfcTask（\*\*(\d+)\*\* 个安装、\*\*(\d+)\*\* 个交付），每个任务带 IfcTaskTime",
          [len(tasks), sum(task.PredefinedType == "CONSTRUCTION" for task in tasks),
           sum(task.PredefinedType == "USERDEFINED" and task.ObjectType == "DELIVERY" for task in tasks)])
    if len(IFC.by_type("IfcTaskTime")) != len(tasks) or any(task.TaskTime is None for task in tasks):
        failures.append("正式 IFC 任务必须逐个有 IfcTaskTime")
    claim("IFC 实体数与校验", r"文件共 \*\*([\d,]+)\*\* 个实体，ifcopenshell 的 schema 校验 \*\*(\d+)\*\* 个问题",
          [len(list(IFC)), validate_issues()])
    claim("仓库结构：检查条数", r"\| (\d+) 条模型检查 \|", [groups["模型"]])
    claim("仓库结构：布置检查条数", r"施工总平面几何与 (\d+) 条布置检查", [groups["工地布置"]])

    if checked[0] != EXPECTED:
        failures.append("核对条数 %d ≠ EXPECTED %d——README 增删了带数字的句子，请同步本脚本" % (checked[0], EXPECTED))
    if failures:
        for f in failures:
            print("MISMATCH", f)
        sys.exit(1)
    print("PASS README 的 %d 处数字全部与产物一致" % checked[0])


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()

"""模型与模型检查。每条检查都配一个故意弄坏的反例：检查必须能失败，才算在检查。

EveryCheckHasACounterexample 枚举 checks.ALL 与 site.run_checks，保证 12 条检查无一遗漏。
"""
import contextlib
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from facade import checks, config as C, site                  # noqa: E402
from facade.model import OUT_X, OUT_Y, build_panels, footprint  # noqa: E402


class PanelSet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panels = build_panels()

    def test_counts_by_type(self):
        counts = {}
        for p in self.panels:
            counts[p.ptype] = counts.get(p.ptype, 0) + 1
        self.assertEqual(counts, {"U1": 88, "U2": 620, "U3": 10, "U4": 2, "U5": 90})
        self.assertEqual(len(self.panels), 2 * (C.N_LONG + C.N_SHORT) * len(C.LEVELS))

    def test_facade_area_is_perimeter_times_height(self):
        """板块模数面积之和 = 外立面周长（不含转角立柱）× 总高。两条独立算法。"""
        area = sum(C.MODULE * (p.h + C.JOINT) for p in self.panels) / 1e6
        top = C.LEVELS[-1][1] + C.LEVELS[-1][2]
        self.assertAlmostEqual(area, 2 * (C.N_LONG + C.N_SHORT) * C.MODULE * top / 1e6, places=6)

    def test_weight_recomputed_from_first_principles(self):
        """标准层单元的吊装重，按分区面积 × 面密度 + 型材长度 × 线密度手算一遍。"""
        p = next(q for q in self.panels if q.ptype == "U2")
        net_w = (p.w - 2 * C.MULLION_W) / 1000.0
        vision = net_w * (p.h - 3 * C.TRANSOM_H - C.SPANDREL_H) / 1000.0
        spandrel = net_w * C.SPANDREL_H / 1000.0
        kg = (vision * 0.012 * 2500 + spandrel * (0.006 * 2500 + 0.003 * 2700 + 0.05 * 100)
              + 2 * p.h / 1000.0 * C.MULLION_KG_M + 3 * net_w * C.TRANSOM_KG_M)
        self.assertAlmostEqual(p.weight_kg, round(kg, 1), places=6)

    def test_panels_hug_the_outline(self):
        """四个立面的外表面恰好落在外轮廓线上。"""
        for p in self.panels:
            x0, y0, x1, y1 = footprint(p)
            face = {"S": y0, "N": OUT_Y - y1, "W": x0, "E": OUT_X - x1}[p.elev]
            self.assertEqual(face, 0.0, p.pid)


class ChecksPassAndCanFail(unittest.TestCase):
    def setUp(self):
        self.panels = build_panels()

    def names_failing(self, panels):
        return [name for name, ok, _ in checks.run_all(panels) if not ok]

    def test_all_pass_on_the_real_model(self):
        self.assertEqual(self.names_failing(self.panels), [])

    def _move(self, pid, dx=0.0, dy=0.0, dz=0.0):
        ps = copy.deepcopy(self.panels)
        p = next(q for q in ps if q.pid == pid)
        p.origin = (p.origin[0] + dx, p.origin[1] + dy, p.origin[2] + dz)
        return ps

    def test_moving_one_panel_5mm_breaks_the_vertical_joint_check(self):
        failing = self.names_failing(self._move("S-L4-12", dx=5))
        self.assertTrue(any("竖缝" in n for n in failing), failing)

    def test_dropping_one_panel_5mm_breaks_the_stack_joint_check(self):
        failing = self.names_failing(self._move("E-L6-03", dz=-5))
        self.assertTrue(any("横缝" in n for n in failing), failing)

    def test_overlapping_panels_are_reported_as_a_clash(self):
        failing = self.names_failing(self._move("N-L2-07", dx=-40))
        self.assertTrue(any("冲突" in n for n in failing), failing)

    def test_duplicate_ids_are_reported(self):
        ps = copy.deepcopy(self.panels)
        ps[5].pid = ps[4].pid
        self.assertTrue(any("编号" in n for n in self.names_failing(ps)))

    def test_an_overweight_panel_fails_the_lift_check(self):
        ps = copy.deepcopy(self.panels)
        ps[0].weight_kg = C.LIFT_SWL_KG + 0.1
        self.assertTrue(any("吊装" in n for n in self.names_failing(ps)))

    def test_an_oversize_pane_fails_the_glass_check(self):
        ps = copy.deepcopy(self.panels)
        ps[0].zones["vision1"] = C.GLASS_MAX_H + 1
        self.assertTrue(any("玻璃" in n for n in self.names_failing(ps)))

    def test_moving_one_panel_outward_breaks_the_envelope_check(self):
        """南立面向外是 -y：把一块板挪出外轮廓线，外轮廓检查必须变红。"""
        failing = self.names_failing(self._move("S-L3-10", dy=-1000))
        self.assertTrue(any("外轮廓" in n for n in failing), failing)


@contextlib.contextmanager
def patched_config(**values):
    """临时改 facade.config 的场地参数；site.py 每次调用都现读 C.*，退出时恢复原值。"""
    old = dict((k, getattr(C, k)) for k in values)
    try:
        for k, v in values.items():
            setattr(C, k, v)
        yield
    finally:
        for k, v in old.items():
            setattr(C, k, v)


def site_ok(peak=10):
    """工地布置检查结果；名称去掉括号里的参数（如「（半径 50 m）」）作键。"""
    return dict((name.split("（")[0], ok) for name, ok, _ in site.run_checks(peak))


class SiteChecks(unittest.TestCase):
    def test_pass_at_the_planned_peak(self):
        self.assertTrue(all(ok for _, ok, _ in site.run_checks(10)))

    def test_laydown_rejects_a_plan_that_does_not_fit(self):
        n = len(site.slots())
        res = dict((name, ok) for name, ok, _ in site.run_checks(n + 1))
        self.assertFalse(res["堆场架位 ≥ 排程峰值"])

    def test_crane_check_fails_if_the_jib_is_too_short(self):
        old = C.CRANE_RADIUS
        try:
            C.CRANE_RADIUS = 45000
            res = dict((name.split("（")[0], ok) for name, ok, _ in site.run_checks(10))
            self.assertFalse(res["塔吊覆盖整栋楼"])
        finally:
            C.CRANE_RADIUS = old

    def test_crane_check_fails_if_a_slot_is_out_of_reach(self):
        """堆场整体东移到红线边：最远架位角超出塔吊半径，其余布置检查仍通过。"""
        with patched_config(**site_breakers()["塔吊覆盖全部架位"][0]):
            res = site_ok()
        self.assertEqual([k for k, ok in res.items() if not ok], ["塔吊覆盖全部架位"])

    def test_laydown_inside_the_drop_zone_fails(self):
        """堆场北移进楼外坠物禁区，只有禁区检查变红。"""
        with patched_config(**site_breakers()["堆场不进坠物禁区"][0]):
            res = site_ok()
        self.assertEqual([k for k, ok in res.items() if not ok], ["堆场不进坠物禁区"])

    def test_anything_outside_the_site_boundary_fails(self):
        """塔吊、堆场、车道任一越出红线，红线检查都必须变红。"""
        cases = {
            "crane": dict(CRANE=(C.SITE[0] - 1000, C.CRANE[1])),
            "laydown": dict(LAYDOWN_ORIGIN=(C.LAYDOWN_ORIGIN[0], C.SITE[1] - 1)),
            "truck route": dict(TRUCK_ROUTE=[(x, y - 1) for x, y in C.TRUCK_ROUTE]),
        }
        for label, values in cases.items():
            with self.subTest(label), patched_config(**values):
                self.assertFalse(site_ok()["堆场、塔吊、车道都在红线内"])


def broken_model(func_name):
    """每条模型检查一个最小破坏，返回弄坏后的板块列表；没有配反例的检查返回 None。"""
    ps = build_panels()

    def shift(pid, dx=0.0, dy=0.0, dz=0.0):
        p = next(q for q in ps if q.pid == pid)
        p.origin = (p.origin[0] + dx, p.origin[1] + dy, p.origin[2] + dz)

    if func_name == "check_unique_ids":
        ps[5].pid = ps[4].pid
    elif func_name == "check_joints":
        shift("S-L4-12", dx=5)
    elif func_name == "check_stack_joints":
        shift("E-L6-03", dz=-5)
    elif func_name == "check_no_clash":
        shift("N-L2-07", dx=-40)
    elif func_name == "check_envelope":
        shift("S-L3-10", dy=-1000)
    elif func_name == "check_glass_size":
        ps[0].zones["vision1"] = C.GLASS_MAX_H + 1
    elif func_name == "check_lift":
        ps[0].weight_kg = C.LIFT_SWL_KG + 0.1
    else:
        return None
    return ps


def site_breakers():
    """工地布置检查名 → (要临时改的 config 参数, 排程峰值)。"""
    return {
        "塔吊覆盖整栋楼": (dict(CRANE_RADIUS=C.CRANE_RADIUS - 5000), 10),
        "塔吊覆盖全部架位": (dict(LAYDOWN_ORIGIN=(C.SITE[2] - C.LAYDOWN_COLS * C.SLOT_L, C.LAYDOWN_ORIGIN[1])), 10),
        "堆场架位 ≥ 排程峰值": ({}, len(site.slots()) + 1),
        "堆场不进坠物禁区": (dict(LAYDOWN_ORIGIN=(C.LAYDOWN_ORIGIN[0], -12000)), 10),
        "堆场、塔吊、车道都在红线内": (dict(CRANE=(C.SITE[0] - 1000, C.CRANE[1])), 10),
    }


class EveryCheckHasACounterexample(unittest.TestCase):
    """README 说「每条检查都有一个故意弄坏的反例证明它会失败」，这里机械地核这句话。

    从 checks.ALL 与 site.run_checks 枚举全部检查：新增一条检查却没配反例，
    或反例不再让它变红，这里就失败。"""

    def test_every_model_check_in_checks_all_has_a_failing_counterexample(self):
        real = build_panels()
        for func in checks.ALL:
            with self.subTest(func.__name__):
                broken = broken_model(func.__name__)
                self.assertIsNotNone(broken, "%s 没有反例" % func.__name__)
                self.assertTrue(func(real)[1], "%s 在真实模型上应通过" % func.__name__)
                self.assertFalse(func(broken)[1], "%s 的反例没能让它失败" % func.__name__)

    def test_every_site_check_has_a_failing_counterexample(self):
        names = sorted(site_ok())
        breakers = site_breakers()
        self.assertEqual(names, sorted(breakers), "工地布置检查与反例表不一致")
        for name in names:
            with self.subTest(name):
                values, peak = breakers[name]
                self.assertTrue(site_ok()[name])
                with patched_config(**values):
                    self.assertFalse(site_ok(peak)[name], "%s 的反例没能让它失败" % name)

    def test_the_count_matches_the_readme_claim(self):
        """README 第一屏：模型检查 7 条 + 工地布置检查 5 条。"""
        self.assertEqual((len(checks.ALL), len(site.run_checks(10))), (7, 5))


if __name__ == "__main__":
    unittest.main()

"""模型与模型检查。每条检查都配一个故意弄坏的反例：检查必须能失败，才算在检查。"""
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


if __name__ == "__main__":
    unittest.main()

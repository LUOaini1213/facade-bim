"""4D 排程与物流的不变量。这些是计划必须满足的约束，不是具体数值。"""
import os
import sys
import unittest
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from facade import config as C, schedule as S, site   # noqa: E402
from facade.pipeline import compute                   # noqa: E402


class Plan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = compute()

    def test_every_panel_installed_exactly_once_in_sequence(self):
        ins = self.r["install"]
        self.assertEqual(sorted(seq for seq, _ in ins.values()), list(range(1, len(self.r["panels"]) + 1)))
        days = [ins[p.pid][1] for p in self.r["panels"]]
        self.assertEqual(days, sorted(days), "安装日必须随安装顺序单调不减")

    def test_daily_hours_never_exceed_crew_capacity(self):
        cap = C.CREWS * C.CREW_HOURS
        hours = Counter()
        for p in self.r["panels"]:
            hours[self.r["install"][p.pid][1]] += C.INSTALL_HOURS[p.ptype]
        self.assertLessEqual(max(hours.values()), cap + 1e-9)
        self.assertTrue(all(S.is_workday(d) for d in hours))

    def test_levels_are_installed_bottom_up(self):
        order = [lv for lv, _, _ in C.LEVELS]
        last = {}
        first = {}
        for p in self.r["panels"]:
            d = self.r["install"][p.pid][1]
            last[p.level] = max(last.get(p.level, d), d)
            first[p.level] = min(first.get(p.level, d), d)
        for a, b in zip(order, order[1:]):
            self.assertLessEqual(last[a], first[b], "%s 未装完就开始 %s" % (a, b))

    def test_stillages_hold_one_type_within_capacity(self):
        types = {p.pid: p.ptype for p in self.r["panels"]}
        seen = Counter()
        for s in self.r["stillages"]:
            self.assertEqual({types[i] for i in s["panels"]}, {s["type"]})
            self.assertLessEqual(s["n"], C.STILLAGE_CAP[s["type"]])
            seen.update(s["panels"])
        self.assertEqual(set(seen), set(types))
        self.assertEqual(set(seen.values()), {1})

    def test_every_stillage_arrives_by_its_due_date(self):
        for s in self.r["stillages"]:
            self.assertLessEqual(s["delivered"], s["due"], s["stillage"])
            self.assertLess(s["delivered"], s["first_install"], s["stillage"])

    def test_trucks_respect_load_and_gate_limits(self):
        per_day = Counter(t["date"] for t in self.r["trucks"])
        self.assertLessEqual(max(per_day.values()), C.TRUCKS_PER_DAY)
        self.assertTrue(all(len(t["stillages"]) <= C.TRUCK_STILLAGES for t in self.r["trucks"]))
        self.assertTrue(all(S.is_workday(d) for d in per_day))

    def test_site_stock_fits_the_drawn_laydown(self):
        self.assertLessEqual(max(n for _, n in self.r["stock"]), len(site.slots()))

    def test_stock_is_counted_against_an_independent_replay(self):
        """按事件重放一遍在场架数：到场 +1、末块装完的次日 -1。"""
        events = Counter()
        for s in self.r["stillages"]:
            events[s["delivered"]] += 1
            events[S.add_workdays(s["last_install"], 1)] -= 1
        n, replay = 0, {}
        for d, _ in self.r["stock"]:
            n += events.pop(d, 0)
            replay[d] = n
        self.assertEqual(replay, dict(self.r["stock"]))


class Sensitivity(unittest.TestCase):
    def test_one_truck_a_day_overflows_the_laydown(self):
        r = compute(trucks_per_day=1)
        self.assertGreater(r["summary"]["peak_stock"], len(site.slots()))

    def test_a_longer_buffer_never_lowers_the_peak(self):
        peaks = [compute(buffer_days=b)["summary"]["peak_stock"] for b in (1, 2, 3)]
        self.assertEqual(peaks, sorted(peaks))


if __name__ == "__main__":
    unittest.main()

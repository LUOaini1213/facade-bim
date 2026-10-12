"""4D 排程与物流的不变量。这些是计划必须满足的约束，不是具体数值。"""
import os
import sys
import unittest
from collections import Counter
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

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


class InstallCapacity(unittest.TestCase):
    @staticmethod
    def panels(count=1, ptype="U4"):
        return [SimpleNamespace(pid="controlled-" + str(i), ptype=ptype) for i in range(count)]

    def test_oversized_panel_is_rejected_without_advancing_to_a_fake_install_day(self):
        with patch.object(S, "add_workdays") as advance:
            with self.assertRaisesRegex(ValueError, "controlled-0.*non-preemptive daily capacity"):
                S.install_plan(self.panels(), crews=1, crew_hours=1)
            advance.assert_not_called()

    def test_none_uses_the_defaults_but_explicit_values_are_respected(self):
        panels = self.panels(3)
        self.assertEqual(S.install_plan(panels), S.install_plan(panels, C.CREWS, C.CREW_HOURS))
        with patch.object(C, "CREWS", 1), patch.object(C, "CREW_HOURS", 1):
            with self.assertRaisesRegex(ValueError, "non-preemptive"):
                S.install_plan(panels, None, None)
            self.assertEqual(S.install_plan(panels, 2, 3)["controlled-0"][1], C.INSTALL_START)

    def test_zero_negative_fractional_boolean_or_nonfinite_crew_counts_are_rejected(self):
        for crews in (0, -1, 1.5, 2.0, True, False, float("nan"), float("inf"), "2"):
            with self.subTest(crews=crews), self.assertRaisesRegex(ValueError, "crews"):
                S.install_plan(self.panels(), crews=crews)

    def test_zero_negative_boolean_or_nonfinite_hours_are_rejected(self):
        for hours in (0, -1, True, False, float("nan"), float("inf"), -float("inf"), "8"):
            with self.subTest(hours=hours), self.assertRaisesRegex(ValueError, "crew_hours"):
                S.install_plan(self.panels(), crew_hours=hours)

    def test_invalid_configured_defaults_are_not_silently_accepted(self):
        with patch.object(C, "CREWS", 0), self.assertRaisesRegex(ValueError, "crews"):
            S.install_plan(self.panels())
        with patch.object(C, "CREW_HOURS", float("nan")), self.assertRaisesRegex(ValueError, "crew_hours"):
            S.install_plan(self.panels())

    def test_capacity_product_and_numeric_conversion_must_remain_finite(self):
        for crews, hours in ((2, 1e308), (10**1000, 8), (1, 10**1000)):
            with self.subTest(crews=crews, hours=hours), self.assertRaisesRegex(ValueError, "finite"):
                S.install_plan(self.panels(), crews=crews, crew_hours=hours)

    def test_bad_task_duration_is_rejected_instead_of_cancelling_or_poisoning_daily_use(self):
        for duration in (0, -1, True, float("nan"), float("inf"), "3"):
            with self.subTest(duration=duration), patch.dict(C.INSTALL_HOURS, {"U4": duration}):
                with self.assertRaisesRegex(ValueError, "install hours for U4"):
                    S.install_plan(self.panels())

    def test_aggregate_crew_hours_allow_an_exact_capacity_task_without_splitting_it(self):
        result = S.install_plan(self.panels(2), crews=2, crew_hours=1.5)
        self.assertEqual(result, {"controlled-0": (1, C.INSTALL_START),
                                  "controlled-1": (2, S.add_workdays(C.INSTALL_START, 1))})

    def test_two_tasks_can_fill_a_day_exactly_and_the_third_moves_as_a_whole(self):
        result = S.install_plan(self.panels(3), crews=2, crew_hours=3)
        self.assertEqual([seq for seq, _ in result.values()], [1, 2, 3])
        self.assertEqual([day for _, day in result.values()],
                         [C.INSTALL_START, C.INSTALL_START, S.add_workdays(C.INSTALL_START, 1)])

    def test_valid_fractional_hours_are_preserved(self):
        result = S.install_plan(self.panels(3, "U5"), crews=1, crew_hours=1)
        self.assertEqual([day for _, day in result.values()],
                         [C.INSTALL_START, C.INSTALL_START, S.add_workdays(C.INSTALL_START, 1)])

    def test_a_new_day_skips_sunday_and_respects_the_same_capacity(self):
        with patch.object(C, "INSTALL_START", date(2026, 11, 7)):
            result = S.install_plan(self.panels(2), crews=1, crew_hours=3)
        self.assertEqual([day for _, day in result.values()], [date(2026, 11, 7), date(2026, 11, 9)])

    def test_empty_input_still_validates_capacity(self):
        self.assertEqual(S.install_plan([], crews=1, crew_hours=8), {})
        with self.assertRaisesRegex(ValueError, "crews"):
            S.install_plan([], crews=0)


if __name__ == "__main__":
    unittest.main()

"""日期回放与真实排程对账，含边界、坏输入和架位复用。"""
from datetime import timedelta
import unittest

from facade.pipeline import compute, panel_rows
from facade.replay import allocate_slots, snapshot


class Replay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = compute()
        cls.rows = panel_rows(cls.result)

    def test_before_delivery_and_after_completion(self):
        first = self.result["summary"]["first_delivery"]
        last = self.result["summary"]["install_last"]
        before = snapshot(self.rows, first - timedelta(days=1))
        after = snapshot(self.rows, last + timedelta(days=1))
        self.assertEqual(before["counts"]["not_delivered"], len(self.rows))
        self.assertEqual(before["occupied_slots"], 0)
        self.assertEqual(after["counts"]["installed"], len(self.rows))
        self.assertEqual(after["occupied_slots"], 0)

    def test_every_workday_matches_existing_stock_and_install_schedule(self):
        for day, stock in self.result["stock"]:
            with self.subTest(day=day):
                state = snapshot(self.rows, day)
                self.assertEqual(state["occupied_slots"], stock)
                self.assertEqual(sum(state["counts"].values()), len(self.rows))
                self.assertEqual(state["counts"]["installing"], sum(
                    d == day for _, d in self.result["install"].values()))
                self.assertEqual(state["visible_panels"], sum(
                    d <= day for _, d in self.result["install"].values()))
                slots = [s["slot"] for s in state["active_stillages"]]
                self.assertEqual(len(slots), len(set(slots)))
                self.assertTrue(all(0 <= s < 12 for s in slots))

    def test_arrival_is_not_installed_geometry(self):
        row = {"pid": "P", "stillage": "ST", "delivery_date": "2026-10-31", "install_date": "2026-11-02"}
        on_site = snapshot([row], "2026-10-31")
        installing = snapshot([row], "2026-11-02")
        installed = snapshot([row], "2026-11-03")
        self.assertEqual(on_site["states"]["P"], "on_site")
        self.assertEqual(on_site["visible_panels"], 0)
        self.assertEqual(installing["states"]["P"], "installing")
        self.assertEqual(installing["occupied_slots"], 1)
        self.assertEqual(installed["states"]["P"], "installed")
        self.assertEqual(installed["occupied_slots"], 0)

    def test_capacity_is_enforced_and_same_day_slots_not_reused(self):
        with self.assertRaisesRegex(ValueError, "堆场超过"):
            snapshot(self.rows, "2026-11-02", capacity=1)
        from datetime import date
        groups = {"A": {"delivery": date(2026, 1, 1), "last_install": date(2026, 1, 2)},
                  "B": {"delivery": date(2026, 1, 2), "last_install": date(2026, 1, 3)}}
        with self.assertRaises(ValueError):
            allocate_slots(groups, 1)
        groups["B"]["delivery"] = date(2026, 1, 3)
        self.assertEqual(allocate_slots(groups, 1), {"A": 0, "B": 0})

    def test_rejects_duplicate_or_late_or_inconsistent_rows(self):
        row = {"pid": "P", "stillage": "ST", "delivery_date": "2026-11-01", "install_date": "2026-11-02"}
        for rows in ([], [row, row], [dict(row, delivery_date="2026-11-03")],
                     [row, dict(row, pid="Q", delivery_date="2026-11-02")]):
            with self.assertRaises(ValueError):
                snapshot(rows, "2026-11-02")


if __name__ == "__main__":
    unittest.main()

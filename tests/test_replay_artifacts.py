"""Read the actual native snapshots and reject scale changes with unchanged coordinates."""
from pathlib import Path
import unittest
from unittest.mock import patch

import rhino3dm
from scripts.check_replay import verify

ROOT = Path(__file__).resolve().parents[1]
DAYS = ("2026-10-30", "2026-11-02", "2026-11-23", "2026-12-20")


class NativeReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = rhino3dm.File3dm.Read(str(ROOT / "model/facade_bim.3dm"))

    def test_all_required_native_snapshots_match_source(self):
        for day in DAYS:
            with self.subTest(day=day):
                verify(ROOT / ("model/replay/facade_" + day + ".3dm"), self.source)

    def test_coordinate_preserving_wrong_units_are_rejected(self):
        path = ROOT / "model/replay/facade_2026-10-30.3dm"
        changed = rhino3dm.File3dm.Read(str(path))
        changed.Settings.ModelUnitSystem = rhino3dm.UnitSystem.Meters
        with patch("scripts.check_replay.rhino3dm.File3dm.Read", return_value=changed):
            with self.assertRaisesRegex(AssertionError, "replay model units changed"):
                verify(path, self.source)

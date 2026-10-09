"""Actual geometry profiles and tampered summaries must not inherit default claims."""
import ast
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import rhino3dm as R

from facade import config as C
from facade.source_integrity import read_source_record
from scripts.check_profile import (compute_profile, documentation_mode, geometry_summary,
                                   verify_profile)
from scripts.check_quality import box_gap, source_boxes

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "model/facade_bim.3dm"


class ActiveProfile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.current = compute_profile()
        cls.record = read_source_record(SOURCE)

    def test_current_profile_is_source_bound_and_summary_check_is_readonly(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile_summary.json"
            path.write_text(json.dumps(self.current, ensure_ascii=False), encoding="utf-8")
            before = path.read_bytes()
            self.assertEqual(verify_profile(path), self.current)
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.current["source_sha256"], self.record["source_sha256"])
        self.assertEqual(self.current["documentation_mode"], documentation_mode(C.COPING_END_JOINT))
        self.assertTrue(self.current["quality"]["clearance_ok"])

    def test_modified_saved_model_gap_is_rejected_by_real_source_readback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile_summary.json"
            wrong = json.loads(json.dumps(self.current))
            wrong["coping"]["measured_model_gaps_mm"]["corner_post"] += 5
            path.write_text(json.dumps(wrong), encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "differs from independently read"):
                verify_profile(path)
            self.assertEqual(path.read_bytes(), before)

    def test_config_change_does_not_relabel_an_unchanged_source_as_custom(self):
        source = R.File3dm.Read(str(SOURCE))
        with self.assertRaisesRegex(ValueError, "does not match actual source"):
            geometry_summary(source, self.record["configuration"], C.COPING_END_JOINT + 4)

    def test_28mm_model_profile_is_measured_from_real_modified_box_and_instances(self):
        source = R.File3dm.Read(str(SOURCE))
        objects = {obj.Attributes.Id: obj for obj in source.Objects}
        roof = next(obj for obj in source.Objects if obj.Attributes.GetUserString("type") == "U5")
        definition = next(d for d in source.InstanceDefinitions if d.Id == roof.Geometry.ParentIdefId)
        cap = objects[list(definition.GetObjectIds())[-1]]
        bbox = cap.Geometry.GetBoundingBox()
        plane = R.Plane(R.Point3d((bbox.Min.X + bbox.Max.X) / 2, 0, 0), R.Vector3d(0, 0, 1))
        target_length = self.record["configuration"]["MODULE"] - 28
        self.assertTrue(cap.Geometry.Transform(R.Transform.Scale(plane, target_length / (bbox.Max.X - bbox.Min.X), 1, 1)))
        value = geometry_summary(source, self.record["configuration"], 28)
        self.assertAlmostEqual(value["fabrication_length_mm"], 1572)
        self.assertAlmostEqual(value["physical_mass_kg_per_panel"], 3.1833)
        self.assertAlmostEqual(value["measured_model_gaps_mm"]["adjacent"], 28)
        self.assertAlmostEqual(value["measured_model_gaps_mm"]["corner_post"], 14)
        self.assertAlmostEqual(value["measured_model_gaps_mm"]["roof_corner"], 14 * 2 ** 0.5)

    def test_custom_readme_branch_identifies_reference_scope_and_calls_profile_verifier(self):
        source = ast.parse((ROOT / "scripts/check_readme.py").read_text(encoding="utf-8"))
        main = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        namespace = {"C": C, "README": (ROOT / "README.md").read_text(encoding="utf-8")}
        exec(compile(ast.Module(body=[main], type_ignores=[]), "actual README entry", "exec"), namespace)
        output = io.StringIO()
        with patch.object(C, "COPING_END_JOINT", 28), patch("scripts.check_profile.verify_profile", return_value={"active_coping_end_joint_mm": 28}) as check, redirect_stdout(output):
            namespace["main"]()
        check.assert_called_once()
        self.assertIn("参考数字不代表当前产物", output.getvalue())
        self.assertNotIn("全部与产物一致", output.getvalue())

    def test_20mm_joint_has_eight_real_post_threshold_boundaries(self):
        source = R.File3dm.Read(str(SOURCE))
        objects = {obj.Attributes.Id: obj for obj in source.Objects}
        roof = [obj for obj in source.Objects if obj.Attributes.GetUserString("type") == "U5"]
        definition = next(d for d in source.InstanceDefinitions if d.Id == roof[0].Geometry.ParentIdefId)
        cap = objects[list(definition.GetObjectIds())[-1]]
        box = cap.Geometry.GetBoundingBox()
        plane = R.Plane(R.Point3d((box.Min.X + box.Max.X) / 2, 0, 0), R.Vector3d(0, 0, 1))
        self.assertTrue(cap.Geometry.Transform(R.Transform.Scale(plane, 1580 / (box.Max.X - box.Min.X), 1, 1)))
        value = geometry_summary(source, self.record["configuration"], 20)
        self.assertAlmostEqual(value["measured_model_gaps_mm"]["corner_post"], 10)
        caps = source_boxes({(obj.Attributes.GetUserString("pid"), "coping"): (cap.Geometry, obj.Geometry.Xform) for obj in roof})
        posts = source_boxes({(str(obj.Attributes.Id), "post"): (obj.Geometry, None) for obj in source.Objects
                              if isinstance(obj.Geometry, R.Brep)
                              and source.Layers.FindIndex(obj.Attributes.LayerIndex).FullPath == "幕墙::转角立柱"})
        near = [box_gap(cap_box, post_box) for cap_box in caps.values() for post_box in posts.values()
                if box_gap(cap_box, post_box) <= 10 + 1e-7]
        self.assertEqual(len(near), 8)
        self.assertTrue(all(abs(gap - 10) < 1e-7 for gap in near))


if __name__ == "__main__":
    unittest.main()

"""A failing geometry report must remain readable and reject incomplete coverage."""
import copy
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
import rhino3dm

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_quality import verify


class QualityReports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = json.loads((ROOT / "model/quality/native_all.json").read_text(encoding="utf-8"))
        model = rhino3dm.File3dm.Read(str(ROOT / "model/facade_bim.3dm"))
        cls.ids = {o.Attributes.GetUserString("pid"): str(o.Attributes.Id) for o in model.Objects
                   if o.Attributes.GetUserString("pid")}
        definitions = {d.Id: d for d in model.InstanceDefinitions}
        objects = {o.Attributes.Id: o for o in model.Objects}
        cls.first_part = {o.Attributes.GetUserString("pid"): objects[definitions[o.Geometry.ParentIdefId].GetObjectIds()[0]].Attributes.Name + "#1"
                          for o in model.Objects if o.Attributes.GetUserString("pid")}

    def report_file(self, folder, content):
        path = Path(folder) / "native_test.json"
        path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
        return path

    def test_documented_relative_path_cli(self):
        result = subprocess.run([sys.executable, "scripts/check_quality.py", "model/quality/native_all.json"],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_actual_native_scope_includes_all_four_corner_posts(self):
        self.assertEqual(self.report["spatial"]["units"], 823)
        self.assertEqual(self.report["spatial"]["solid_parts"], 7477)
        self.assertTrue(self.report["model_fixture"]["ok"])

    def test_native_boundary_correction_retains_unapproved_contact(self):
        q = self.report["spatial"]
        self.assertEqual(q["clearance_counts"], {"at_clearance": 252, "contact": 98,
                                                "below_clearance": 0, "threshold_candidate": 0})
        self.assertEqual(q["blocking_clearance_events"], 98)
        self.assertFalse(q["clearance_ok"])
        self.assertTrue(self.report["fixtures"]["boundary_checks"]["ok"])

    def test_contact_cannot_be_relabelled_as_a_boundary(self):
        content = copy.deepcopy(self.report)
        event = next(e for e in content["spatial"]["clearance_events"] if e["kind"] == "contact")
        event["kind"] = "at_clearance"
        content["spatial"]["clearance_counts"]["contact"] -= 1
        content["spatial"]["clearance_counts"]["at_clearance"] += 1
        content["spatial"]["blocking_clearance_events"] -= 1
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(AssertionError, "kind differs"):
            verify(self.report_file(folder, content))

    def test_deleting_all_contacts_cannot_make_clearance_pass(self):
        content = copy.deepcopy(self.report)
        q = content["spatial"]
        q["clearance_events"] = [e for e in q["clearance_events"] if e["kind"] == "at_clearance"]
        q["clearance_counts"]["contact"] = 0
        q["blocking_clearance_events"] = 0
        q["clearance_ok"] = True
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(AssertionError, "missing or extra"):
            verify(self.report_file(folder, content))

    def test_failed_report_retains_clash_and_unresolved_locations(self):
        content = copy.deepcopy(self.report)
        q = content["spatial"]
        contact = next(e for e in q["clearance_events"] if e["kind"] == "contact")
        q["clearance_events"].remove(contact)
        q["clearance_counts"]["contact"] -= 1
        q["blocking_clearance_events"] -= 1
        event = {key: contact[key] for key in ("a", "b", "a_guid", "b_guid", "a_part", "b_part")}
        content["ok"] = content["spatial"]["ok"] = content["spatial"]["clearance_ok"] = False
        content["spatial"]["clashes"] = [dict(event, point_mm=[190, 0, 0], intersection_volume_mm3=100)]
        content["spatial"]["unresolved"] = [dict(event, reason="deliberate kernel failure fixture")]
        with tempfile.TemporaryDirectory() as folder:
            path = self.report_file(folder, content)
            self.assertFalse(verify(path)["ok"])
            page = path.with_suffix(".html").read_text(encoding="utf-8")
            for text in ("VOLUME CLASH", "UNRESOLVED", event["a"], "deliberate kernel failure fixture"):
                self.assertIn(text, page)
            content["ok"] = content["spatial"]["ok"] = content["spatial"]["clearance_ok"] = True
            with self.assertRaises(AssertionError):
                verify(self.report_file(folder, content))

    def test_unresolved_pair_with_invalid_source_identity_is_rejected(self):
        content = copy.deepcopy(self.report)
        q = content["spatial"]
        contact = next(e for e in q["clearance_events"] if e["kind"] == "contact")
        q["clearance_events"].remove(contact)
        q["clearance_counts"]["contact"] -= 1
        q["blocking_clearance_events"] -= 1
        q["unresolved"] = [dict(contact, a_guid="not-a-source-guid", reason="kernel failure")]
        content["ok"] = q["ok"] = q["clearance_ok"] = False
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(AssertionError, "identity not in source"):
            verify(self.report_file(folder, content))

    def test_missing_contact_can_remain_a_located_unresolved_failure(self):
        content = copy.deepcopy(self.report)
        q = content["spatial"]
        contact = next(e for e in q["clearance_events"] if e["kind"] == "contact")
        q["clearance_events"].remove(contact)
        q["clearance_counts"]["contact"] -= 1
        q["blocking_clearance_events"] -= 1
        q["unresolved"] = [{key: contact[key] for key in ("a", "b", "a_guid", "b_guid", "a_part", "b_part")}]
        q["unresolved"][0]["reason"] = "MeshClash returned null"
        content["ok"] = q["ok"] = q["clearance_ok"] = False
        with tempfile.TemporaryDirectory() as folder:
            path = self.report_file(folder, content)
            self.assertFalse(verify(path)["ok"])
            page = path.with_suffix(".html").read_text(encoding="utf-8")
            for text in ("UNRESOLVED", contact["a"], contact["b"], "MeshClash returned null"):
                self.assertIn(text, page)
            content["ok"] = q["ok"] = True
            with self.assertRaises(AssertionError):
                verify(self.report_file(folder, content))

    def test_incomplete_native_scope_is_rejected(self):
        content = copy.deepcopy(self.report)
        content["spatial"]["units"] -= 4
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(AssertionError):
                verify(self.report_file(folder, content))

    def test_required_native_stages_cannot_be_omitted(self):
        for missing in ("fixtures", "spatial", "timeline"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as folder:
                content = copy.deepcopy(self.report)
                del content[missing]
                content["ok"] = True
                with self.assertRaisesRegex(AssertionError, "missing required native quality stages"):
                    verify(self.report_file(folder, content))


if __name__ == "__main__":
    unittest.main()

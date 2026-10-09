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
from scripts.check_quality import verify, verify_box_event_coverage, verify_clearance_event


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

    def pop_clearance_event(self, content):
        q = content["spatial"]
        event = q["clearance_events"].pop(0)
        q["clearance_counts"][event["kind"]] -= 1
        q["blocking_clearance_events"] -= int(event["kind"] != "at_clearance")
        return event

    def test_documented_relative_path_cli(self):
        result = subprocess.run([sys.executable, "scripts/check_quality.py", "model/quality/native_all.json", "--no-write-reports"],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_readonly_verification_does_not_create_html(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.report_file(folder, self.report)
            before = path.read_bytes()
            self.assertTrue(verify(path, write_reports=False)["ok"])
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(path.with_suffix(".html").exists())

    def test_actual_native_scope_includes_all_four_corner_posts(self):
        self.assertEqual(self.report["spatial"]["units"], 823)
        self.assertEqual(self.report["spatial"]["solid_parts"], 7477)
        self.assertTrue(self.report["model_fixture"]["ok"])

    def test_real_end_joint_repair_clears_contacts_without_relaxing_threshold(self):
        # Re-enumerate the complete source-box event set. A legal 20mm end
        # joint creates additional threshold boundaries; none may be omitted.
        checked = verify(ROOT / "model/quality/native_all.json", write_reports=False)
        q = checked["spatial"]
        self.assertEqual({name: q["clearance_counts"][name] for name in ("contact", "below_clearance", "threshold_candidate")},
                         {"contact": 0, "below_clearance": 0, "threshold_candidate": 0})
        self.assertEqual(q["clearance_counts"]["at_clearance"], len(q["clearance_events"]))
        self.assertEqual(q["blocking_clearance_events"], 0)
        self.assertTrue(q["clearance_ok"])
        self.assertEqual(q["clearance_mm"], 10)
        self.assertTrue(self.report["fixtures"]["boundary_checks"]["ok"])

    def test_contact_cannot_be_relabelled_as_a_boundary(self):
        event = copy.deepcopy(self.report["fixtures"]["diagnostics"][4]["clearance_events"][0])
        bounds = [[[0, 0, 0], [10, 10, 10]], [[10, 0, 0], [20, 10, 10]]]
        self.assertEqual(verify_clearance_event(event, bounds, 2, 0.01), "contact")
        event["kind"] = "at_clearance"
        with self.assertRaisesRegex(AssertionError, "kind differs"):
            verify_clearance_event(event, bounds, 2, 0.01)

    def test_deleting_all_contacts_cannot_make_clearance_pass(self):
        event = self.report["fixtures"]["diagnostics"][4]["clearance_events"][0]
        proven = {(event["a"], event["a_part"]): [[0, 0, 0], [10, 10, 10]],
                  (event["b"], event["b_part"]): [[10, 0, 0], [20, 10, 10]]}
        verify_box_event_coverage(proven, [event], 2)
        with self.assertRaisesRegex(AssertionError, "missing or extra"):
            verify_box_event_coverage(proven, [], 2)

    def test_deleting_current_boundary_records_is_still_rejected(self):
        content = copy.deepcopy(self.report)
        q = content["spatial"]
        q["clearance_events"] = []
        q["clearance_counts"] = {key: 0 for key in q["clearance_counts"]}
        q["blocking_clearance_events"] = 0
        q["clearance_ok"] = True
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(AssertionError, "missing or extra"):
            verify(self.report_file(folder, content))

    def test_failed_report_retains_clash_and_unresolved_locations(self):
        content = copy.deepcopy(self.report)
        q = content["spatial"]
        contact = self.pop_clearance_event(content)
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
        contact = self.pop_clearance_event(content)
        q["unresolved"] = [dict(contact, a_guid="not-a-source-guid", reason="kernel failure")]
        content["ok"] = q["ok"] = q["clearance_ok"] = False
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(AssertionError, "identity not in source"):
            verify(self.report_file(folder, content))

    def test_missing_clearance_event_can_remain_a_located_unresolved_failure(self):
        content = copy.deepcopy(self.report)
        q = content["spatial"]
        contact = self.pop_clearance_event(content)
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

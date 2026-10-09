"""Regress stale outputs, interrupted rebuilds and use of the selected Python."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ENTRY = Path(__file__).resolve().parents[1] / "scripts/delivery.py"
SPEC = importlib.util.spec_from_file_location("delivery_entry", ENTRY)
delivery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery)


class DeliveryIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name, content in {"facade/config.py": "JOINT=20\n", "README.md": "example\n",
                              "requirements.txt": "rhino3dm==8.32.0\n",
                              "model/facade_bim.3dm": "source",
                              "model/replay/facade_2026-12-20.json": "snapshot"}.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.record = {"schema": 1, "hash_policy": delivery.HASH_POLICY, "project": "facade", "state": "passed", "construction_config": None,
                       "inputs": delivery.inputs(self.root, "facade"),
                       "artifacts": delivery.artifacts(self.root, "facade"),
                       "build_environment": {"rhino_version": "test-fixture"}}
        delivery.write_index(self.root, self.record)

    def test_changed_parameter_rejects_existing_success(self):
        (self.root / "facade/config.py").write_text("JOINT=24\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inputs/code"):
            delivery.verify_index(self.root, "facade")

    def test_git_line_ending_conversion_does_not_make_inputs_stale(self):
        (self.root / "facade/config.py").write_bytes(b"JOINT=20\r\n")
        self.assertEqual(delivery.verify_index(self.root, "facade")["state"], "passed")

    def test_modified_source_rejects_unchanged_report(self):
        (self.root / "model/facade_bim.3dm").write_text("modified source", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "artifacts"):
            delivery.verify_index(self.root, "facade")

    def test_missing_snapshot_cannot_be_omitted_from_check(self):
        (self.root / "model/replay/facade_2026-12-20.json").unlink()
        with self.assertRaisesRegex(ValueError, "artifacts"):
            delivery.verify_index(self.root, "facade")

    def test_failed_rebuild_does_not_leave_passed_index(self):
        with patch.object(delivery, "preflight_native"), patch.object(delivery, "run", side_effect=RuntimeError("native stopped")):
            with self.assertRaisesRegex(RuntimeError, "native stopped"):
                delivery.execute(self.root, rebuild=True)
        record = json.loads((self.root / delivery.INDEX).read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "failed")
        with self.assertRaisesRegex(ValueError, "incomplete or failed"):
            delivery.verify_index(self.root, "facade")

    def test_missing_rhino_preserves_index_and_source(self):
        before = delivery.digest(self.root / delivery.INDEX)
        source = delivery.digest(self.root / "model/facade_bim.3dm")
        with patch.object(delivery, "preflight_native", side_effect=ValueError("Rhino missing")):
            with self.assertRaisesRegex(ValueError, "Rhino missing"):
                delivery.execute(self.root, rebuild=True)
        self.assertEqual(before, delivery.digest(self.root / delivery.INDEX))
        self.assertEqual(source, delivery.digest(self.root / "model/facade_bim.3dm"))

    def test_rebuild_rejects_concurrent_parameter_edit(self):
        def editing_run(*args):
            (self.root / "facade/config.py").write_text("JOINT=25\n", encoding="utf-8")
        with patch.object(delivery, "preflight_native"), patch.object(delivery, "run", side_effect=editing_run):
            with self.assertRaisesRegex(ValueError, "inputs changed during rebuild"):
                delivery.execute(self.root, rebuild=True)
        self.assertEqual(json.loads((self.root / delivery.INDEX).read_text(encoding="utf-8"))["state"], "failed")

    def test_child_uses_selected_environment_interpreter(self):
        script = self.root / "record_interpreter.py"
        script.write_text("import pathlib,sys\npathlib.Path('python.txt').write_text(sys.executable)\n", encoding="utf-8")
        delivery.run(self.root, [[script.name]])
        self.assertEqual((self.root / "python.txt").read_text(), sys.executable)

    def test_external_construction_profile_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "inside this repository"):
            delivery.resolve_config(self.root, ENTRY)


if __name__ == "__main__":
    unittest.main()

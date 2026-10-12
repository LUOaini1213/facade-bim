"""Regress stale outputs, interrupted rebuilds and use of the selected Python."""
import importlib.util
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

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
        self.real_committed_digest = delivery.committed_baseline_digest
        anchor = patch.object(delivery, "committed_baseline_digest", return_value=delivery.baseline_digest(self.record))
        self.anchor = anchor.start()
        self.addCleanup(anchor.stop)

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

    def revalidate(self, checking=None, commit="a" * 40):
        result = type("Commit", (), {"stdout": commit})()
        with patch.object(delivery.subprocess, "run", return_value=result), \
                patch.object(delivery, "run", side_effect=checking):
            return delivery.revalidate_delivery(self.root, "facade")

    def test_read_only_refresh_keeps_the_original_build_identity_and_hashes_current_code(self):
        (self.root / "facade/config.py").write_text("JOINT=20\n# validation-only change\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inputs/code"):
            delivery.verify_index(self.root, "facade")
        result = self.revalidate()
        self.assertEqual({k: v for k, v in result.items() if k != "read_only_verification"}, self.record)
        verification = result["read_only_verification"]
        self.assertFalse(verification["native_execution"])
        self.assertNotIn("rhino_version", verification["environment"])
        self.assertEqual(verification["checks"], delivery.commands("facade"))
        self.assertEqual(verification["inputs"], delivery.inputs(self.root, "facade"))
        self.assertEqual(verification["artifacts"], self.record["artifacts"])
        self.assertNotEqual(verification["inputs"], self.record["inputs"])

    def test_read_only_refresh_cannot_bless_a_changed_native_artifact(self):
        previous = (self.root / delivery.INDEX).read_bytes()
        (self.root / "model/facade_bim.3dm").write_text("changed native artifact", encoding="utf-8")
        checks = Mock()
        with self.assertRaisesRegex(ValueError, "artifacts"):
            self.revalidate(checks)
        checks.assert_not_called()
        self.assertEqual((self.root / delivery.INDEX).read_bytes(), previous)

    def test_read_only_check_failure_preserves_the_previous_index(self):
        previous = (self.root / delivery.INDEX).read_bytes()
        with self.assertRaisesRegex(RuntimeError, "failed actual checker"):
            self.revalidate(RuntimeError("failed actual checker"))
        self.assertEqual((self.root / delivery.INDEX).read_bytes(), previous)

    def test_read_only_refresh_rejects_input_changes_during_the_checks(self):
        previous = (self.root / delivery.INDEX).read_bytes()
        def editing_check(*args):
            (self.root / "facade/config.py").write_text("JOINT=30\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inputs changed during read-only"):
            self.revalidate(editing_check)
        self.assertEqual((self.root / delivery.INDEX).read_bytes(), previous)

    def test_read_only_refresh_rejects_artifact_changes_during_the_checks(self):
        previous = (self.root / delivery.INDEX).read_bytes()
        def editing_check(*args):
            (self.root / "model/facade_bim.3dm").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "artifacts changed during read-only"):
            self.revalidate(editing_check)
        self.assertEqual((self.root / delivery.INDEX).read_bytes(), previous)

    def test_read_only_refresh_does_not_overwrite_a_concurrent_index_change(self):
        def editing_check(*args):
            (self.root / delivery.INDEX).write_text("concurrent edit", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "index changed during read-only"):
            self.revalidate(editing_check)
        self.assertEqual((self.root / delivery.INDEX).read_text(), "concurrent edit")

    def test_read_only_receipt_cannot_hide_a_drifted_original_build_identity(self):
        result = self.revalidate()
        result["build_environment"]["rhino_version"] = "invented new build"
        delivery.write_index(self.root, result)
        with self.assertRaisesRegex(ValueError, "Invalid read-only verification"):
            delivery.verify_index(self.root, "facade")

    def test_read_only_receipt_requires_complete_checks_inputs_and_artifact_identity(self):
        original = self.revalidate()
        for field, value in (("checks", []), ("inputs", {}), ("artifacts", {}),
                             ("native_execution", True), ("source_base_commit", "not-a-commit"),
                             ("verified_at", "not-a-timestamp")):
            with self.subTest(field=field):
                altered = deepcopy(original)
                altered["read_only_verification"][field] = value
                delivery.write_index(self.root, altered)
                with self.assertRaises(ValueError):
                    delivery.verify_index(self.root, "facade")

    def test_read_only_verified_source_change_still_invalidates_the_index(self):
        self.revalidate()
        (self.root / "facade/config.py").write_text("JOINT=21\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inputs/code"):
            delivery.verify_index(self.root, "facade")

    def test_read_only_verified_artifact_change_still_invalidates_the_index(self):
        self.revalidate()
        (self.root / "model/facade_bim.3dm").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "artifacts"):
            delivery.verify_index(self.root, "facade")

    def test_refresh_cannot_reanchor_an_edited_original_environment(self):
        altered = deepcopy(self.record)
        altered["build_environment"]["rhino_version"] = "invented new build"
        delivery.write_index(self.root, altered)
        previous = (self.root / delivery.INDEX).read_bytes()
        checks = Mock()
        with self.assertRaisesRegex(ValueError, "committed baseline"):
            self.revalidate(checks)
        checks.assert_not_called()
        self.assertEqual((self.root / delivery.INDEX).read_bytes(), previous)

    def test_check_cannot_reanchor_the_original_environment_by_rewriting_both_hashes(self):
        result = self.revalidate()
        result["build_environment"]["rhino_version"] = "invented new build"
        result["read_only_verification"]["baseline_record_sha256"] = delivery.baseline_digest(result)
        delivery.write_index(self.root, result)
        with self.assertRaisesRegex(ValueError, "committed baseline"):
            delivery.verify_index(self.root, "facade")

    def test_refresh_cannot_bless_changed_artifact_bytes_by_rewriting_original_hashes(self):
        (self.root / "model/facade_bim.3dm").write_text("changed native bytes", encoding="utf-8")
        altered = deepcopy(self.record)
        altered["artifacts"] = delivery.artifacts(self.root, "facade")
        delivery.write_index(self.root, altered)
        checks = Mock()
        with self.assertRaisesRegex(ValueError, "committed baseline"):
            self.revalidate(checks)
        checks.assert_not_called()

    def test_original_baseline_is_read_from_a_real_fixed_git_blob(self):
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", delivery.INDEX], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "-c", "commit.gpgsign=false", "commit", "-qm", "fixture baseline"], cwd=self.root, check=True)
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root,
                                capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(self.real_committed_digest(self.root, commit), delivery.baseline_digest(self.record))
        (self.root / delivery.INDEX).write_text("working-tree edit", encoding="utf-8")
        self.assertEqual(self.real_committed_digest(self.root, commit), delivery.baseline_digest(self.record))

    def test_subsequent_refresh_keeps_the_first_anchor_when_the_source_head_moves(self):
        self.revalidate()
        result = self.revalidate(commit="b" * 40)
        self.assertEqual(result["read_only_verification"]["baseline_commit"], "a" * 40)
        self.assertEqual(result["read_only_verification"]["source_base_commit"], "b" * 40)
        self.assertEqual(self.anchor.call_args[0][1], "a" * 40)

    def test_new_head_cannot_launder_an_existing_original_build_identity(self):
        altered = self.revalidate()
        altered["build_environment"]["rhino_version"] = "invented new build"
        altered["read_only_verification"]["baseline_record_sha256"] = delivery.baseline_digest(altered)
        delivery.write_index(self.root, altered)
        previous = (self.root / delivery.INDEX).read_bytes()
        old_digest, new_digest = delivery.baseline_digest(self.record), delivery.baseline_digest(altered)
        with patch.object(delivery, "committed_baseline_digest",
                          side_effect=lambda root, commit: old_digest if commit == "a" * 40 else new_digest):
            with self.assertRaisesRegex(ValueError, "committed baseline"):
                self.revalidate(commit="b" * 40)
        self.assertEqual((self.root / delivery.INDEX).read_bytes(), previous)

    def test_record_and_drift_comparison_use_the_same_original_byte_snapshot(self):
        original_read = delivery.read_index
        edited = deepcopy(self.record)
        edited["build_environment"]["rhino_version"] = "concurrent external edit"
        edited_bytes = json.dumps(edited).encode("utf-8")
        def editing_read(root, project, config=None, raw=None):
            record = original_read(root, project, config, raw=raw)
            (root / delivery.INDEX).write_bytes(edited_bytes)
            return record
        with patch.object(delivery, "read_index", side_effect=editing_read):
            with self.assertRaisesRegex(ValueError, "index changed during read-only"):
                self.revalidate()
        self.assertEqual((self.root / delivery.INDEX).read_bytes(), edited_bytes)

    def child_entry(self):
        return ("import importlib.util, pathlib, sys, os\n"
                "s=importlib.util.spec_from_file_location('child_delivery', " + repr(str(ENTRY)) + ")\n"
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
                "m.ROOT=pathlib.Path(" + repr(str(self.root)) + ")\n")

    def test_real_cooperating_cli_writer_is_refused_while_the_repo_lock_is_held(self):
        import subprocess
        previous = (self.root / delivery.INDEX).read_bytes()
        body = self.child_entry() + "sys.argv=['delivery.py','--revalidate']; m.main()\n"
        with delivery.index_writer_lock(self.root):
            result = subprocess.run([sys.executable, "-c", body], capture_output=True,
                                    text=True, encoding="utf-8", timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Another delivery index writer is active", result.stderr)
        self.assertEqual((self.root / delivery.INDEX).read_bytes(), previous)
        delivery.write_index(self.root, self.record)  # The released lock is usable.

    def test_os_releases_the_lock_after_a_real_holder_crashes(self):
        import subprocess
        body = self.child_entry() + "with m.index_writer_lock(m.ROOT): os._exit(7)\n"
        result = subprocess.run([sys.executable, "-c", body], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 7)
        self.assertTrue((self.root / ".delivery_index.lock").is_file())
        delivery.write_index(self.root, self.record)
        self.assertEqual(delivery.read_index(self.root, "facade"), self.record)

    def test_failed_atomic_replace_cleans_distinct_temp_files_and_preserves_the_index(self):
        previous = (self.root / delivery.INDEX).read_bytes()
        with patch.object(delivery.os, "replace", side_effect=PermissionError("busy destination")) as replace:
            for _ in range(2):
                with self.assertRaises(PermissionError):
                    delivery.write_index(self.root, self.record)
        paths = [call.args[0] for call in replace.call_args_list]
        self.assertNotEqual(paths[0], paths[1])
        self.assertFalse(list((self.root / "model").glob("delivery_index.json.*.tmp")))
        self.assertEqual((self.root / delivery.INDEX).read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()

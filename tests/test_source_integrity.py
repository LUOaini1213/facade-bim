"""Real source/config counterexamples must fail before any native model write."""
import ast
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import rhino3dm

from facade import config as C
from facade.model import coping_span
from facade.source_integrity import coping_update_rows, read_source_record

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "model/facade_bim.3dm"


class SourceIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        model = rhino3dm.File3dm.Read(str(SOURCE))
        cls.records = [(dict(obj.Attributes.GetUserStrings()),
                        [[float(getattr(obj.Geometry.Xform, "M%d%d" % (i, j))) for j in range(4)] for i in range(4)])
                       for obj in model.Objects if isinstance(obj.Geometry, rhino3dm.InstanceReference)
                       and obj.Attributes.GetUserString("pid")]

    def test_joint24_counterexample_stops_real_repair_before_native_read_or_write(self):
        tree = ast.parse((ROOT / "rhino/repair_coping_joints.py").read_text(encoding="utf-8"))
        repair = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "repair")
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "model").mkdir()
            copied = directory / "model/facade_bim.3dm"
            copied.write_bytes(SOURCE.read_bytes())
            (copied.parent / "source_config.json").write_bytes((SOURCE.parent / "source_config.json").read_bytes())
            before = hashlib.sha256(copied.read_bytes()).hexdigest()
            native_read, native_write = Mock(), Mock()
            namespace = {"ROOT": str(directory), "os": SimpleNamespace(path=__import__("os").path, replace=native_write),
                         "legacy": SimpleNamespace(digest=lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()),
                         "read_source_record": read_source_record,
                         "Rhino": SimpleNamespace(FileIO=SimpleNamespace(File3dm=SimpleNamespace(Read=native_read)))}
            exec(compile(ast.Module(body=[repair], type_ignores=[]), "actual repair entry point", "exec"), namespace)
            with patch.object(C, "JOINT", 24), self.assertRaisesRegex(ValueError, "source/configuration mismatch.*JOINT"):
                namespace["repair"]()
            native_read.assert_not_called()
            native_write.assert_not_called()
            self.assertEqual(hashlib.sha256(copied.read_bytes()).hexdigest(), before)

    def test_source_dimensions_reject_the_same_mixing_without_manifest(self):
        with patch.object(C, "JOINT", 24), self.assertRaisesRegex(ValueError, "source/configuration mismatch"):
            coping_update_rows(self.records)

    def test_end_joint28_uses_source_dimensions_for_geometry_and_quantity(self):
        with patch.object(C, "COPING_END_JOINT", 28):
            read_source_record(SOURCE)
            updates = coping_update_rows(self.records)
            width = float(next(values for values, _ in self.records if values["type"] == "U5")["w_mm"])
            start, end = coping_span(width)
        self.assertEqual((start, end), (4, 1576))
        self.assertEqual(len(updates), 90)
        self.assertEqual({row["coping_alu_kg"] for row in updates.values()}, {"3.183"})
        self.assertAlmostEqual((end - start) * 250 * 3 * 2700 / 1e9, 3.1833)

    def test_panel_transform_corruption_is_not_hidden_by_matching_usertext(self):
        records = deepcopy(self.records)
        records[0][1][0][3] += 0.01
        with self.assertRaisesRegex(ValueError, "transform mismatch"):
            coping_update_rows(records)

    def test_non_coping_material_configuration_cannot_partially_update_source(self):
        for name, value in (("ALU_DENSITY", 2800), ("COPING_MM", 4), ("MODULE", 1604)):
            with self.subTest(name=name), patch.object(C, name, value), self.assertRaisesRegex(ValueError, name):
                read_source_record(SOURCE)

    def test_stale_source_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            copied = Path(folder) / "facade_bim.3dm"
            copied.write_bytes(b"different source")
            (copied.parent / "source_config.json").write_bytes((SOURCE.parent / "source_config.json").read_bytes())
            with self.assertRaisesRegex(ValueError, "does not identify"):
                read_source_record(copied)

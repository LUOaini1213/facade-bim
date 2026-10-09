"""Actual Rhino instance positions expose the old four-corner coping clash."""
import ast
import hashlib
import itertools
import json
from pathlib import Path
import unittest

import rhino3dm as R

from facade import config as C
from facade.model import zones_for

ROOT = Path(__file__).resolve().parents[1]


def source_roof():
    model = R.File3dm.Read(str(ROOT / "model/facade_bim.3dm"))
    objects = {obj.Attributes.Id: obj for obj in model.Objects}
    panels = [obj for obj in model.Objects if obj.Attributes.GetUserString("pid") and
              obj.Attributes.GetUserString("type") == "U5"]
    definitions = {definition.Id: definition for definition in model.InstanceDefinitions}
    definition = definitions[panels[0].Geometry.ParentIdefId]
    target = objects[list(definition.GetObjectIds())[-1]]
    assert target.Attributes.Name == "alu"
    assert len(target.Geometry.Faces) == 6 and len(target.Geometry.Vertices) == 8
    return model, panels, definition, target


def builder_coping():
    # Execute the real geometry-producing function without importing the
    # build script, whose module entry point intentionally rebuilds Rhino.
    source = ast.parse((ROOT / "rhino/build_model.py").read_text(encoding="utf-8"))
    node = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "panel_parts")
    def box(x0, y0, z0, x1, y1, z1):
        return R.Brep.CreateFromBox(R.Box(R.BoundingBox(R.Point3d(x0, y0, z0), R.Point3d(x1, y1, z1))))
    namespace = {"C": C, "zones_for": zones_for, "box": box}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "panel_parts", "exec"), namespace)
    return namespace["panel_parts"]("U5", 1580, 1180)[-1][1]


def corner_volumes(panels, coping):
    # The inspected input is an eight-corner cuboid, so intersection of its
    # transformed bounds is exact for the real cardinal instance rotations.
    bounds = []
    for panel in panels:
        shape = coping.Duplicate()
        assert shape.Transform(panel.Geometry.Xform)
        bounds.append((panel.Attributes.GetUserString("pid"), shape.GetBoundingBox()))
    events = []
    for (aid, a), (bid, b) in itertools.combinations(bounds, 2):
        lengths = [max(0, min(getattr(a.Max, axis), getattr(b.Max, axis)) -
                       max(getattr(a.Min, axis), getattr(b.Min, axis))) for axis in "XYZ"]
        volume = lengths[0] * lengths[1] * lengths[2]
        if volume > 0.001:
            events.append((aid, bid, volume))
    return events


def panel_identity(model):
    rows = {}
    for obj in model.Objects:
        pid = obj.Attributes.GetUserString("pid")
        if not pid or not isinstance(obj.Geometry, R.InstanceReference):
            continue
        transform = obj.Geometry.Xform
        rows[pid] = {"guid": str(obj.Attributes.Id), "definition": str(obj.Geometry.ParentIdefId),
                     "attributes": dict(obj.Attributes.GetUserStrings()),
                     "transform": [float(getattr(transform, "M%d%d" % (row, column)))
                                   for row in range(4) for column in range(4)]}
    text = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return len(rows), hashlib.sha256(text.encode("utf-8")).hexdigest()


class RoofCoping(unittest.TestCase):
    def test_actual_builder_back_edge_width_and_corner_clearance(self):
        coping = builder_coping()
        bbox = coping.GetBoundingBox()
        self.assertEqual((bbox.Min.Y, bbox.Max.Y), (-70, 180))
        self.assertEqual(bbox.Max.Y - bbox.Min.Y, 250)
        self.assertEqual(bbox.Max.Z - bbox.Min.Z, 50)
        self.assertEqual(corner_volumes(source_roof()[1], coping), [])

    def test_old_offset_counterexample_reproduces_four_actual_clashes(self):
        _, panels, _, target = source_roof()
        old = target.Geometry.Duplicate()
        self.assertTrue(old.Translate(R.Vector3d(0, 210 - old.GetBoundingBox().Max.Y, 0)))
        events = corner_volumes(panels, old)
        self.assertEqual(len(events), 4)
        for _, _, volume in events:
            self.assertAlmostEqual(volume, 45000, places=6)

    def test_committed_source_coping_has_no_corner_volume_penetration(self):
        _, panels, _, target = source_roof()
        bbox = target.Geometry.GetBoundingBox()
        self.assertEqual((bbox.Min.Y, bbox.Max.Y), (-70, 180))
        self.assertEqual(corner_volumes(panels, target.Geometry), [])

    def test_native_repair_report_preserves_all_panel_identities(self):
        model, _, definition, target = source_roof()
        report = json.loads((ROOT / "model/quality/roof_coping_repair.json").read_text(encoding="utf-8"))
        self.assertTrue(report["ok"])
        self.assertEqual(report["source_after_sha256"], hashlib.sha256((ROOT / "model/facade_bim.3dm").read_bytes()).hexdigest())
        count, identity = panel_identity(model)
        self.assertEqual((report["panel_count"], report["panel_identity_sha256"]), (count, identity))
        self.assertEqual(report["target_guid"], str(target.Attributes.Id))
        self.assertEqual(report["definition_guid"], str(definition.Id))
        self.assertEqual(report["preserved_object_guids"], len(model.Objects))
        self.assertEqual(report["unchanged_object_attributes"], len(model.Objects))
        self.assertEqual(report["definitions_preserved"], len(model.InstanceDefinitions))
        self.assertEqual(report["after_corners"]["clashes"], [])
        self.assertEqual(report["after_corners"]["unresolved"], [])


if __name__ == "__main__":
    unittest.main()

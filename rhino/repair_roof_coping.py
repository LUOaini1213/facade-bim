#! python3
"""Translate the one shared U5 coping in place; retain every model GUID.

Uses native RhinoCommon File3dm, not a rebuild of the 810 panel instances.
Only after rereading/auditing a candidate and checking its real corner Breps
does this script replace model/facade_bim.3dm. Repeating it is a no-op.
"""
import hashlib
import json
import os
import shutil
import sys
import traceback
import uuid

import Rhino
import Rhino.Geometry as RG

ROOT = os.environ.get("FACADE_BIM_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for directory in (ROOT, os.path.join(ROOT, "rhino")):
    if directory not in sys.path:
        sys.path.insert(0, directory)
from facade import config as C
import spatial_quality as spatial


def digest(path):
    with open(path, "rb") as stream:
        return hashlib.sha256(stream.read()).hexdigest()


def encoded(component):
    options = Rhino.FileIO.SerializationOptions()
    options.RhinoVersion = 8
    options.WriteUserData = True
    options.WriteRenderMeshes = False
    options.WriteAnalysisMeshes = False
    return hashlib.sha256(component.ToJSON(options).encode("utf-8")).hexdigest()


def inventory(model):
    return {"objects": {str(obj.Attributes.ObjectId): {
                "geometry": encoded(obj.Geometry), "attributes": encoded(obj.Attributes)} for obj in model.Objects},
            "definitions": {str(definition.Id): encoded(definition) for definition in model.AllInstanceDefinitions},
            "layers": {str(layer.Id): encoded(layer) for layer in model.AllLayers},
            "materials": {str(material.Id): encoded(material) for material in model.AllMaterials}}


def panel_identity(model):
    rows = {}
    for obj in model.Objects:
        attrs = obj.Attributes
        pid = attrs.GetUserString("pid")
        if not pid or not isinstance(obj.Geometry, RG.InstanceReferenceGeometry):
            continue
        strings = attrs.GetUserStrings()
        transform = obj.Geometry.Xform
        rows[pid] = {"guid": str(attrs.ObjectId), "definition": str(obj.Geometry.ParentIdefId),
                     "attributes": {key: strings.Get(key) for key in strings.AllKeys},
                     "transform": [float(getattr(transform, "M%d%d" % (row, column)))
                                   for row in range(4) for column in range(4)]}
    text = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return len(rows), hashlib.sha256(text.encode("utf-8")).hexdigest()


def roof(model):
    objects = {obj.Attributes.ObjectId: obj for obj in model.Objects}
    definitions = {definition.Id: definition for definition in model.AllInstanceDefinitions}
    panels = [obj for obj in model.Objects if obj.Attributes.GetUserString("type") == "U5"
              and obj.Attributes.GetUserString("pid") and isinstance(obj.Geometry, RG.InstanceReferenceGeometry)]
    assert len(panels) == 90, "expected the existing 90 U5 panels"
    definition_ids = {obj.Geometry.ParentIdefId for obj in panels}
    assert len(definition_ids) == 1, "U5 must retain one shared definition"
    definition = definitions[next(iter(definition_ids))]
    assert definition.Name == "U5"
    parts = [objects[identifier] for identifier in definition.GetObjectIds()]
    assert len(parts) == 8
    w, h = float(panels[0].Attributes.GetUserString("w_mm")), float(panels[0].Attributes.GetUserString("h_mm"))
    assert all(float(obj.Attributes.GetUserString("w_mm")) == w and
               float(obj.Attributes.GetUserString("h_mm")) == h for obj in panels)
    candidates = [obj for obj in parts if obj.Attributes.Name == "alu" and
                  abs(obj.Geometry.GetBoundingBox(True).Min.Z - h) < 1e-6 and
                  abs(obj.Geometry.GetBoundingBox(True).Max.Z - h - 50) < 1e-6]
    assert len(candidates) == 1, "cannot identify the unique U5 coping"
    target = candidates[0]
    bbox = target.Geometry.GetBoundingBox(True)
    assert abs(bbox.Min.X + bbox.Max.X - w) < 1e-6, "coping must remain centered on the panel"
    assert 0 < bbox.Max.X - bbox.Min.X <= w + C.JOINT + 1e-6
    assert abs(bbox.Max.Y - bbox.Min.Y - C.COPING_W) < 1e-6
    assert target.Geometry.IsValid and target.Geometry.IsSolid
    return panels, target, definition


def corner_check(model):
    panels, target, _ = roof(model)
    units = []
    for obj in panels:
        geometry = target.Geometry.DuplicateBrep()
        assert geometry.Transform(obj.Geometry.Xform)
        units.append(spatial.unit(obj.Attributes.GetUserString("pid"),
                                  [spatial.part(geometry, "alu#8 coping")], str(obj.Attributes.ObjectId)))
    return spatial.analyse(units, 10, 0.1)


def verify_unchanged(before, after, target_id, changed):
    assert before.keys() == after.keys()
    for table in ("definitions", "layers", "materials"):
        assert before[table] == after[table], table + " changed"
    assert before["objects"].keys() == after["objects"].keys(), "object GUIDs changed"
    for identifier, original in before["objects"].items():
        actual = after["objects"][identifier]
        assert original["attributes"] == actual["attributes"], "object attributes changed: " + identifier
        if identifier != target_id or not changed:
            assert original["geometry"] == actual["geometry"], "unrelated geometry changed: " + identifier
        else:
            assert original["geometry"] != actual["geometry"], "target geometry was not updated"


def repair():
    source = os.path.join(ROOT, "model", "facade_bim.3dm")
    before_sha = digest(source)
    model = Rhino.FileIO.File3dm.Read(source)
    assert model is not None and model.Settings.ModelUnitSystem == Rhino.UnitSystem.Millimeters
    before = inventory(model)
    identity = panel_identity(model)
    assert identity[0] == 810
    panels, target, definition = roof(model)
    target_id = str(target.Attributes.ObjectId)
    bbox = target.Geometry.GetBoundingBox(True)
    corrected = abs(bbox.Min.Y - (C.DEPTH - C.COPING_W)) < 1e-6 and abs(bbox.Max.Y - C.DEPTH) < 1e-6
    if not corrected:
        assert abs(bbox.Min.Y + 40) < 1e-6 and abs(bbox.Max.Y - (C.COPING_W - 40)) < 1e-6, "unexpected coping offset"
    old_volume = spatial._mass(target.Geometry)[0]
    old_quality = corner_check(model)
    assert not old_quality["unresolved"]
    if corrected:
        assert not old_quality["clashes"]
    else:
        assert len(old_quality["clashes"]) == 4 and all(
            abs(event["intersection_volume_mm3"] - 45000) < 1e-6 for event in old_quality["clashes"])
        # RhinoCommon's File3dmObject.Geometry is a const-backed view: mutating
        # it can detach a private copy without changing the containing model.
        # Replace just this table entry, explicitly reusing its original UUID.
        replacement = target.Geometry.DuplicateBrep()
        attributes = target.Attributes.Duplicate()
        original_id = attributes.ObjectId
        assert replacement.Translate(RG.Vector3d(0, C.DEPTH - (C.COPING_W - 40), 0))
        assert model.Objects.Delete(original_id), "cannot remove the coping table entry"
        replacement_id = model.Objects.AddBrep(replacement, attributes)
        assert replacement_id == original_id, "native replacement did not retain the coping GUID"
        panels, target, definition = roof(model)
    new_volume = spatial._mass(target.Geometry)[0]
    assert abs(new_volume - old_volume) <= old_volume * 1e-10, "coping volume/physical mass changed"
    assert panel_identity(model) == identity
    if corrected:
        # Native mass/mesh queries can populate Brep caches after Read. The
        # no-op branch never writes this in-memory model; prove the source's
        # actual file bytes were preserved instead of comparing cache bytes.
        assert digest(source) == before_sha, "no-op repair changed the source file"
    else:
        verify_unchanged(before, inventory(model), target_id, True)
    candidate_quality = corner_check(model)
    assert candidate_quality["ok"] and not candidate_quality["clashes"] and not candidate_quality["unresolved"]
    backup = os.path.join(os.path.dirname(ROOT), "output", "rhino_backups", "facade_bim_before_coping.3dm")
    if not corrected:
        os.makedirs(os.path.dirname(backup), exist_ok=True)
        if os.path.exists(backup):
            assert digest(backup) == before_sha, "existing backup differs; refusing to overwrite it"
        else:
            shutil.copyfile(source, backup)
        neutral = os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Documents", "facade-bim", "roof-repair")
        os.makedirs(neutral, exist_ok=True)
        candidate = os.path.join(neutral, "facade_bim_" + uuid.uuid4().hex + ".3dm")
        staged = source + ".roof-repair.tmp"
        try:
            assert model.Write(candidate, 8), "native File3dm candidate write failed"
            reread = Rhino.FileIO.File3dm.Read(candidate)
            assert reread is not None
            verify_unchanged(before, inventory(reread), target_id, True)
            assert panel_identity(reread) == identity
            reread_quality = corner_check(reread)
            assert reread_quality["ok"] and not reread_quality["clashes"] and not reread_quality["unresolved"]
            assert digest(source) == before_sha, "source changed while the repair was running"
            shutil.copyfile(candidate, staged)
            assert digest(staged) == digest(candidate)
            os.replace(staged, source)
        finally:
            if os.path.exists(candidate):
                os.remove(candidate)
            if os.path.exists(staged):
                os.remove(staged)
    return {"ok": True, "changed": not corrected, "rhino": str(Rhino.RhinoApp.Version),
            "source_before_sha256": before_sha, "source_after_sha256": digest(source),
            "backup": os.path.relpath(backup, ROOT).replace("\\", "/"),
            "target_guid": target_id, "definition_guid": str(definition.Id), "affected_panels": len(panels),
            "panel_identity_sha256": identity[1], "panel_count": identity[0],
            "preserved_object_guids": len(before["objects"]), "unchanged_object_attributes": len(before["objects"]),
            "unchanged_other_geometries": len(before["objects"]) - (not corrected),
            "definitions_preserved": len(before["definitions"]),
            "coping_width_mm": C.COPING_W, "back_edge_mm": C.DEPTH,
            "outside_projection_mm": C.COPING_W - C.DEPTH, "coping_volume_mm3": new_volume,
            "before_corners": old_quality, "after_corners": candidate_quality}


def main():
    try:
        report = repair()
    except Exception:
        report = {"ok": False, "error": traceback.format_exc()}
    destination = os.path.join(ROOT, "model", "quality", "roof_coping_repair.json")
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return report


if __name__ == "__main__":
    main()

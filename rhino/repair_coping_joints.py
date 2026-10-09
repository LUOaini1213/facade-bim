#! python3
"""Apply configurable U5 end joints without rebuilding any source identity."""
import hashlib
import json
import os
import shutil
import sys
import traceback
import uuid
import System

import Rhino
import Rhino.Geometry as RG

ROOT = os.environ.get("FACADE_BIM_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for directory in (ROOT, os.path.join(ROOT, "rhino")):
    if directory not in sys.path:
        sys.path.insert(0, directory)
from facade import config as C
from facade.model import coping_span
from facade.source_integrity import (ALLOWED_COPING_ATTRIBUTES, coping_update_rows,
                                     read_source_record, write_source_record)
import repair_roof_coping as legacy
import spatial_quality as spatial

ALLOWED = ALLOWED_COPING_ATTRIBUTES
ATTRIBUTE_ARCHIVE_NORMALIZATION = set()
USER_STRING_LIST_ID = System.Guid("CE28DE29-F4C5-4FAA-A50A-C3A6849B6329")


def non_string_attributes_hash(attributes):
    copied = attributes.Duplicate()
    copied.DeleteAllUserStrings()
    assert copied.UserStringCount == 0
    # OpenNURBS keeps an empty native UserStringList with a mutation copy-count
    # in its archive. Remove only this positively identified empty container
    # on the audit copy. No plugin userdata is purged or ignored.
    # https://github.com/mcneel/opennurbs/blob/8.x/opennurbs_userdata.cpp
    if copied.UserData.Count == 1 and copied.UserData.Contains(USER_STRING_LIST_ID):
        copied.UserData.Purge()
    return {"hash": legacy.encoded(copied), "remaining_userdata": copied.UserData.Count,
            "userdata_types": [{"type": str(item.GetType()), "description": str(item.Description)} if item is not None else "native opaque userdata" for item in copied.UserData]}


def attribute_properties(attributes, view_ids=()):
    values = {}
    for prop in attributes.GetType().GetProperties():
        # UserDictionary's getter allocates new plugin data; audit it through
        # the archived userdata guard instead of mutating an audit copy.
        if prop.Name == "UserDictionary":
            continue
        if prop.CanRead and prop.GetIndexParameters().Length == 0:
            try:
                values[prop.Name] = str(prop.GetValue(attributes, None))
            except Exception as error:
                values[prop.Name] = "getter error: " + str(error)
    values["groups"] = [int(value) for value in attributes.GetGroupList() or []]
    values["display_modes"] = {str(ident): str(attributes.GetDisplayModeOverride(ident)) for ident in view_ids}
    return values


def audit(before, after, target_id, old_props, expected_rows):
    actual = legacy.inventory(after)
    for table in ("definitions", "layers", "materials"):
        assert before[table] == actual[table], table + " identities/properties changed"
    assert before["objects"].keys() == actual["objects"].keys(), "source object GUIDs changed"
    for obj in after.Objects:
        ident = str(obj.Attributes.ObjectId)
        original = before["objects"][ident]
        if ident in old_props:
            attrs = obj.Attributes.Duplicate()
            pid = attrs.GetUserString("pid")
            strings = attrs.GetUserStrings()
            values = {key: strings.Get(key) for key in strings.AllKeys}
            expected = dict(old_props[ident]["_all_user_strings"])
            for key in ALLOWED:
                assert attrs.GetUserString(key) == str(expected_rows[pid][key]), "U5 take-off property differs"
                expected[key] = str(expected_rows[pid][key])
            assert values == expected, "unrelated U5 UserText changed: " + ident
            normalized = non_string_attributes_hash(attrs)
            old_normalized = old_props[ident]["_non_string_attributes_hash"]
            if normalized != old_normalized:
                assert normalized["remaining_userdata"] == old_normalized["remaining_userdata"] == 0, "non-UserText plugin userdata changed: " + json.dumps({"before": old_normalized, "after": normalized}, ensure_ascii=False)
                properties = attribute_properties(attrs, old_props[ident]["_view_ids"])
                old = old_props[ident]["_properties"]
                delta = {key: [old.get(key), properties.get(key)] for key in set(old).union(properties) if old.get(key) != properties.get(key)}
                assert not delta, "unrelated U5 attribute changed: " + ident + " property delta=" + json.dumps(delta, ensure_ascii=False)
                ATTRIBUTE_ARCHIVE_NORMALIZATION.add(ident)
        else:
            assert actual["objects"][ident]["attributes"] == original["attributes"], "unrelated attribute changed: " + ident
        if ident != target_id:
            assert actual["objects"][ident]["geometry"] == original["geometry"], "unrelated geometry/transform changed"
    panels, target, _ = legacy.roof(after)
    w = float(panels[0].Attributes.GetUserString("w_mm"))
    x0, x1 = coping_span(w)
    bounds = target.Geometry.GetBoundingBox(True)
    assert abs(bounds.Min.X - x0) < 1e-7 and abs(bounds.Max.X - x1) < 1e-7
    assert abs(bounds.Min.Y - (C.DEPTH - C.COPING_W)) < 1e-7 and abs(bounds.Max.Y - C.DEPTH) < 1e-7
    assert spatial._box_proof(spatial.part(target.Geometry.DuplicateBrep(), "coping"))


def cap_quality(model):
    panels, target, _ = legacy.roof(model)
    units = []
    for obj in panels:
        geometry = target.Geometry.DuplicateBrep()
        assert geometry.Transform(obj.Geometry.Xform)
        units.append(spatial.unit(obj.Attributes.GetUserString("pid"),
                                  [spatial.part(geometry, "alu#8 coping")], str(obj.Attributes.ObjectId)))
    layers = {layer.Index: layer for layer in model.AllLayers}
    for obj in model.Objects:
        if layers[obj.Attributes.LayerIndex].FullPath == "幕墙::转角立柱":
            units.append(spatial.unit(obj.Attributes.Name or str(obj.Attributes.ObjectId), [spatial.part(obj.Geometry.DuplicateBrep(), "corner post")],
                                      str(obj.Attributes.ObjectId), "corner_post"))
    assert len(units) == 94
    return spatial.analyse(units, 10, 0.1)


def repair():
    source = os.path.join(ROOT, "model", "facade_bim.3dm")
    before_sha = legacy.digest(source)
    # Fail closed before any object deletion, backup or model write. The input
    # record is source-bound; panel dimensions/transforms are checked separately.
    read_source_record(source)
    model = Rhino.FileIO.File3dm.Read(source)
    assert model is not None and model.Settings.ModelUnitSystem == Rhino.UnitSystem.Millimeters
    records = []
    for obj in model.Objects:
        if isinstance(obj.Geometry, RG.InstanceReferenceGeometry) and obj.Attributes.GetUserString("pid"):
            strings = obj.Attributes.GetUserStrings()
            records.append(({key: strings.Get(key) for key in strings.AllKeys},
                            [[float(getattr(obj.Geometry.Xform, "M%d%d" % (row, column)))
                              for column in range(4)] for row in range(4)]))
    rows = coping_update_rows(records)
    before = legacy.inventory(model)
    panels, target, definition = legacy.roof(model)
    target_id = str(target.Attributes.ObjectId)
    w, h = float(panels[0].Attributes.GetUserString("w_mm")), float(panels[0].Attributes.GetUserString("h_mm"))
    x0, x1 = coping_span(w)
    bounds = target.Geometry.GetBoundingBox(True)
    assert abs(bounds.Min.Y - (C.DEPTH - C.COPING_W)) < 1e-7 and abs(bounds.Max.Y - C.DEPTH) < 1e-7, "run roof offset repair first"
    previous_length = bounds.Max.X - bounds.Min.X
    before_quality = cap_quality(model)
    assert not before_quality["unresolved"], "cannot diagnose source geometry"
    old_props = {}
    view_ids = [System.Guid.Empty] + [view.Viewport.Id for view in list(model.AllViews) + list(model.AllNamedViews)]
    for obj in panels:
        strings = obj.Attributes.GetUserStrings()
        old_props[str(obj.Attributes.ObjectId)] = {
            "_all_user_strings": {key: strings.Get(key) for key in strings.AllKeys},
            "_properties": attribute_properties(obj.Attributes.Duplicate(), view_ids), "_view_ids": view_ids,
            "_non_string_attributes_hash": non_string_attributes_hash(obj.Attributes)}
    geometry_changed = abs(bounds.Min.X - x0) > 1e-7 or abs(bounds.Max.X - x1) > 1e-7
    if geometry_changed:
        attrs = target.Attributes.Duplicate()
        replacement = RG.Box(RG.BoundingBox(RG.Point3d(x0, bounds.Min.Y, h), RG.Point3d(x1, bounds.Max.Y, h + 50))).ToBrep()
        assert model.Objects.Delete(attrs.ObjectId)
        assert model.Objects.AddBrep(replacement, attrs) == attrs.ObjectId, "coping UUID changed"
    changed_attributes = 0
    for obj in panels:
        pid = obj.Attributes.GetUserString("pid")
        if all(obj.Attributes.GetUserString(key) == str(rows[pid][key]) for key in ALLOWED):
            continue
        attrs = obj.Attributes.Duplicate()
        geometry = obj.Geometry.Duplicate()
        for key in ALLOWED:
            attrs.SetUserString(key, str(rows[pid][key]))
        assert model.Objects.Delete(attrs.ObjectId)
        assert model.Objects.AddInstanceObject(geometry, attrs) == attrs.ObjectId, "panel UUID changed"
        changed_attributes += 1
    audit(before, model, target_id, old_props, rows)
    after_quality = cap_quality(model)
    assert after_quality["ok"] and after_quality["clearance_ok"], "configured joints fail the unchanged 10mm model clearance"
    changed = geometry_changed or changed_attributes > 0
    backup = os.path.join(os.path.dirname(ROOT), "output", "rhino_backups", "facade_bim_before_joint_" + before_sha[:12] + ".3dm")
    if changed:
        os.makedirs(os.path.dirname(backup), exist_ok=True)
        if os.path.exists(backup):
            assert legacy.digest(backup) == before_sha, "backup identity differs"
        else:
            shutil.copyfile(source, backup)
        neutral = os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Documents", "facade-bim", "coping-joints")
        os.makedirs(neutral, exist_ok=True)
        candidate = os.path.join(neutral, "facade_bim_" + uuid.uuid4().hex + ".3dm")
        staged = source + ".coping-joints.tmp"
        try:
            assert model.Write(candidate, 8), "native candidate save failed"
            reread = Rhino.FileIO.File3dm.Read(candidate)
            audit(before, reread, target_id, old_props, rows)
            reread_quality = cap_quality(reread)
            assert reread_quality["ok"] and reread_quality["clearance_ok"]
            assert legacy.digest(source) == before_sha, "source changed concurrently"
            shutil.copyfile(candidate, staged)
            assert legacy.digest(candidate) == legacy.digest(staged)
            os.replace(staged, source)
            write_source_record(source)
        finally:
            if os.path.exists(candidate):
                os.remove(candidate)
            if os.path.exists(staged):
                os.remove(staged)
    report = {"ok": True, "rhino": str(Rhino.RhinoApp.Version), "changed": changed,
              "source_before_sha256": before_sha, "source_after_sha256": legacy.digest(source),
              "preserved_object_guids": len(before["objects"]), "preserved_definition_guids": len(before["definitions"]),
              "preserved_layers": len(before["layers"]), "preserved_materials": len(before["materials"]),
              "target_guid": target_id, "definition_guid": str(definition.Id), "affected_panels": 90,
              "allowed_attribute_keys": list(ALLOWED), "changed_attribute_objects": changed_attributes,
              "attribute_archive_normalization_objects": sorted(ATTRIBUTE_ARCHIVE_NORMALIZATION),
              "attribute_audit": "all UserText equal except permitted keys; full public properties/groups/display modes equal; non-string userdata archive identical or proven empty",
              "old_coping_length_mm": previous_length, "coping_length_mm": x1 - x0,
              "coping_end_joint_mm": C.COPING_END_JOINT, "coping_width_mm": C.COPING_W,
              "physical_sheet_thickness_mm": C.COPING_MM, "visual_envelope_height_mm": 50,
              "physical_coping_kg_per_panel": C.COPING_W * (x1 - x0) * C.COPING_MM * C.ALU_DENSITY / 1e9,
              "assumption": "fictional demonstration joint; not an engineering recommendation; no sealant or corner cover detail",
              "before_caps_and_posts": before_quality, "after_caps_and_posts": after_quality,
              "backup": os.path.relpath(backup, ROOT).replace("\\", "/")}
    # Refresh the original offset-repair audit against this current source;
    # it performs no geometry change once the back edge is already corrected.
    legacy_report = legacy.repair()
    assert legacy_report["ok"] and not legacy_report["changed"]
    with open(os.path.join(ROOT, "model", "quality", "roof_coping_repair.json"), "w", encoding="utf-8") as stream:
        json.dump(legacy_report, stream, ensure_ascii=False, indent=2)
    return report


def main():
    try:
        result = repair()
    except Exception:
        result = {"ok": False, "error": traceback.format_exc()}
    destination = os.path.join(ROOT, "model", "quality", "coping_joint_repair.json")
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, "w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    return result


if __name__ == "__main__":
    main()

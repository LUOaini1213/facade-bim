"""Write or independently verify the active end-joint delivery summary.

Default operation is read-only. --write produces model/profile_summary.json
after the native quality run and IFC/data exports. README numbers describe the
24mm reference delivery; this summary describes the active source model.
"""
import argparse
from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ifcopenshell
import rhino3dm

from facade import config as C
from facade.source_integrity import coping_update_rows, read_source_record
from scripts.check_ifc import verify as verify_ifc
from scripts.check_quality import box_gap, source_box_bounds, source_boxes, verify as verify_quality

REFERENCE_END_JOINT_MM = 24.0
SUMMARY = ROOT / "model/profile_summary.json"


def documentation_mode(end_joint):
    return "default_24mm" if end_joint == REFERENCE_END_JOINT_MM else "custom_profile"


def geometry_summary(source, config, end_joint):
    """Prove the actual coping box and measure transformed source interfaces."""
    if isinstance(end_joint, bool) or not math.isfinite(end_joint) or end_joint <= 0:
        raise ValueError("invalid active coping end joint")
    objects = {obj.Attributes.Id: obj for obj in source.Objects}
    definitions = {definition.Id: definition for definition in source.InstanceDefinitions}
    roof = [obj for obj in source.Objects if isinstance(obj.Geometry, rhino3dm.InstanceReference)
            and obj.Attributes.GetUserString("type") == "U5"]
    if not roof:
        raise ValueError("source has no actual roof panel instances")
    first = roof[0]
    h, w = float(first.Attributes.GetUserString("h_mm")), float(first.Attributes.GetUserString("w_mm"))
    candidates = [objects[ident] for ident in definitions[first.Geometry.ParentIdefId].GetObjectIds()
                  if objects[ident].Attributes.Name == "alu"
                  and abs(objects[ident].Geometry.GetBoundingBox().Min.Z - h) < 1e-7]
    if len(candidates) != 1:
        raise ValueError("source does not identify one actual coping")
    cap = candidates[0]
    bounds = source_box_bounds(cap.Geometry)
    if bounds is None:
        raise ValueError("source coping is not a certified solid box")
    length, width, envelope = (bounds[1][i] - bounds[0][i] for i in range(3))
    if (abs(w - (config["MODULE"] - config["JOINT"])) > 1e-7
            or abs(length - (config["MODULE"] - end_joint)) > 1e-7
            or abs(bounds[0][0] + bounds[1][0] - w) > 1e-7
            or abs(width - config["COPING_W"]) > 1e-7
            or abs(bounds[1][1] - config["DEPTH"]) > 1e-7
            or abs(envelope - 50) > 1e-7):
        raise ValueError("active end-joint/configuration does not match actual source coping dimensions")
    parts = {}
    for obj in roof:
        if obj.Geometry.ParentIdefId != first.Geometry.ParentIdefId:
            raise ValueError("roof panels do not share the actual coping definition")
        parts[(obj.Attributes.GetUserString("pid"), "coping")] = (cap.Geometry, obj.Geometry.Xform)
    post = next(obj for obj in source.Objects if isinstance(obj.Geometry, rhino3dm.Brep)
                and source.Layers.FindIndex(obj.Attributes.LayerIndex).FullPath == "幕墙::转角立柱"
                and abs(obj.Geometry.GetBoundingBox().Min.X) < 1e-7 and abs(obj.Geometry.GetBoundingBox().Min.Y) < 1e-7)
    parts[("southwest_post", "post")] = (post.Geometry, None)
    world = source_boxes(parts)
    if any(value is None for value in world.values()):
        raise ValueError("source interfaces lack certified box geometry")
    density, thickness = config["ALU_DENSITY"], config["COPING_MM"]
    mass = length * width * thickness * density / 1e9
    return {"panel_count": len(roof), "actual_span_x_mm": [bounds[0][0], bounds[1][0]],
            "fabrication_length_mm": length, "fabrication_width_mm": width,
            "physical_sheet_thickness_mm": thickness, "envelope_height_mm": envelope,
            "physical_mass_kg_per_panel": mass, "physical_mass_kg_total": mass * len(roof),
            "measured_model_gaps_mm": {
                "adjacent": box_gap(world[("S-RF-01", "coping")], world[("S-RF-02", "coping")]),
                "corner_post": box_gap(world[("S-RF-01", "coping")], world[("southwest_post", "post")]),
                "roof_corner": box_gap(world[("S-RF-01", "coping")], world[("W-RF-15", "coping")])}}


def _text_digest(path):
    # Git may normalize tracked text on checkout; identities of binary 3dm
    # files are never normalized. The policy is explicit in the summary.
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def compute_profile():
    source_path = ROOT / "model/facade_bim.3dm"
    record = read_source_record(source_path)
    source = rhino3dm.File3dm.Read(str(source_path))
    if source is None or source.Settings.ModelUnitSystem != rhino3dm.UnitSystem.Millimeters:
        raise ValueError("source model is unreadable or its units changed")
    coping = geometry_summary(source, record["configuration"], C.COPING_END_JOINT)
    panels = [obj for obj in source.Objects if isinstance(obj.Geometry, rhino3dm.InstanceReference)
              and obj.Attributes.GetUserString("pid")]
    rows = [(dict(obj.Attributes.GetUserStrings()),
             [[float(getattr(obj.Geometry.Xform, "M%d%d" % (i, j))) for j in range(4)] for i in range(4)]) for obj in panels]
    expected_updates = coping_update_rows(rows)
    with (ROOT / "data/panels.csv").open(encoding="utf-8-sig", newline="") as stream:
        csv_records = list(csv.DictReader(stream))
        csv_rows = {row["pid"]: row for row in csv_records}
    if len(csv_rows) != len(csv_records):
        raise ValueError("active profile CSV has duplicate panel identities")
    if set(csv_rows) != {values["pid"] for values, _ in rows}:
        raise ValueError("active profile CSV panel identity set differs from source")
    for values, _ in rows:
        if csv_rows[values["pid"]] != values:
            raise ValueError("active profile CSV differs from actual source: " + values["pid"])
        if values["pid"] in expected_updates and any(values[key] != value for key, value in expected_updates[values["pid"]].items()):
            raise ValueError("active source fabrication quantities do not match the active joint")
    with (ROOT / "data/takeoff.csv").open(encoding="utf-8-sig", newline="") as stream:
        takeoff = {row["type"]: row for row in csv.DictReader(stream)}
    for name in ("U5", "合计"):
        if takeoff[name]["coping_alu_kg"] != "%.2f" % coping["physical_mass_kg_total"]:
            raise ValueError("active profile coping take-off differs from actual fabrication geometry")
    f = ifcopenshell.open(str(ROOT / "model/facade_bim.ifc"))
    verify_ifc(f, source, config_record=record)
    native = verify_quality(ROOT / "model/quality/native_all.json", write_reports=False)
    quality = native["spatial"]
    if quality["clearance_mm"] != 10 or not native["ok"] or not quality["clearance_ok"]:
        raise ValueError("active profile fails the unchanged 10mm native quality requirement")
    counts = Counter(event["kind"] for event in quality["clearance_events"])
    dependencies = ("model/source_config.json", "model/facade_bim.ifc", "data/panels.csv", "data/takeoff.csv", "model/quality/native_all.json")
    return {"schema": 1, "source_sha256": record["source_sha256"],
            "dependency_hash_policy": "UTF8_TEXT_CRLF_TO_LF_SHA256; source 3dm uses exact binary SHA256",
            "dependencies_text_lf_sha256": {name: _text_digest(ROOT / name) for name in dependencies},
            "active_coping_end_joint_mm": float(C.COPING_END_JOINT),
            "documentation_mode": documentation_mode(C.COPING_END_JOINT),
            "readme_reference_end_joint_mm": REFERENCE_END_JOINT_MM,
            "coping": coping, "coping_takeoff_display_kg": {name: takeoff[name]["coping_alu_kg"] for name in ("U5", "合计")},
            "quality": {"units": quality["units"], "solid_parts": quality["solid_parts"],
                        "required_clearance_mm": quality["clearance_mm"], "volume_clashes": len(quality["clashes"]),
                        "clearance_counts": {name: counts[name] for name in ("at_clearance", "below_clearance", "contact", "threshold_candidate")},
                        "unresolved": len(quality["unresolved"]), "clearance_ok": quality["clearance_ok"]},
            "basis": "fictional demonstration profile; model geometry, not field measurement or approved waterproof detailing"}


def verify_profile(path=SUMMARY):
    if not path.is_file():
        raise ValueError("missing model/profile_summary.json; generate after full native/export checks with --write")
    actual = json.loads(path.read_text(encoding="utf-8"))
    expected = compute_profile()
    if json.dumps(actual, sort_keys=True, allow_nan=False) != json.dumps(expected, sort_keys=True, allow_nan=False):
        raise ValueError("active profile summary differs from independently read source/IFC/data/native quality")
    return expected


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="Generate summary after complete native quality and exports")
    mode.add_argument("--check", action="store_true", help="Read-only independent check (default)")
    args = parser.parse_args()
    try:
        if args.write:
            value = compute_profile()
            staged = SUMMARY.with_suffix(".json.tmp")
            with staged.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n")
            staged.replace(SUMMARY)
        else:
            value = verify_profile()
        print("PASS active profile %.9gmm; %s; model/profile_summary.json" % (value["active_coping_end_joint_mm"], value["documentation_mode"]))
    except (AssertionError, ValueError, OSError, KeyError) as error:
        parser.exit(1, "FAIL " + str(error) + "\n")


if __name__ == "__main__":
    main()

"""独立读取 .3dm 与 IFC：核对真实零件、材料、刚体定位和日粒度任务。

不 import 导出器或 facade 参数。几何引擎实际解析每个共享零件表示，比较
八个顶点与闭合网格体积；再逐个核对所有子件定位/映射，覆盖全部板块。
父板的几何由子件组成，父板不重复持有 Body 或材料；计重仍取源 UserText。
"""
import argparse
from collections import Counter, defaultdict
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import sys
import uuid

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.guid
import ifcopenshell.util.element as element
import ifcopenshell.util.placement as placement
import ifcopenshell.util.unit as unit
import rhino3dm

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = uuid.UUID("5b1f7a1e-2f0c-4b9e-9d1a-6f0a3c2e7b10")


def _need(condition, message):
    if not condition:
        raise AssertionError(message)


def _close_matrix(actual, expected, message):
    _need(all(math.isclose(actual[i][j], expected[i][j], abs_tol=1e-7, rel_tol=0.0)
              for i in range(4) for j in range(4)), message)


def _rhino_matrix(transform):
    return [[getattr(transform, "M%d%d" % (i, j)) for j in range(4)] for i in range(4)]


def _source_parts(source):
    objects = {obj.Attributes.Id: obj for obj in source.Objects}
    return {definition.Id: [objects[oid] for oid in definition.GetObjectIds()]
            for definition in source.InstanceDefinitions}


def _material(source, obj):
    return source.Materials[obj.Attributes.MaterialIndex]


def source_configuration(source_path):
    """Read the source-bound input witness without importing exporter/config."""
    source_path = Path(source_path)
    record = json.loads((source_path.parent / "source_config.json").read_text(encoding="utf-8"))
    _need(record.get("schema") == 1 and record.get("allowed_repair_parameter") == "COPING_END_JOINT"
          and record.get("source_sha256") == hashlib.sha256(source_path.read_bytes()).hexdigest(),
          "source configuration record does not identify the actual source")
    for name in ("COPING_MM", "COPING_W", "ALU_DENSITY"):
        value = record.get("configuration", {}).get(name)
        _need(type(value) in (int, float) and math.isfinite(value) and value > 0,
              "invalid source-bound fabrication input: " + name)
    return record


def check_coping_semantics(child, source_part, source_props, record):
    """Independently recompute physical values from source bounds + input witness."""
    box = source_part.Geometry.GetBoundingBox()
    is_coping = (source_props["type"] == "U5" and source_part.Attributes.Name == "alu"
                 and abs(box.Min.Z - float(source_props["h_mm"])) < 1e-7)
    part = element.get_pset(child, "FacadeBIM_Part") or {}
    _need(part.get("PartRole") == ("ROOF_COPING" if is_coping else "RHINO_BLOCK_PART")
          and part.get("GeometryRepresentation") == ("CONSTRUCTION_ENVELOPE" if is_coping else "SOURCE_GEOMETRY"),
          "part role/envelope semantics differ from actual Rhino part: " + child.Name)
    props = element.get_pset(child, "FacadeBIM_Coping")
    if not is_coping:
        _need(props is None, "non-coping part claims coping fabrication semantics: " + child.Name)
        return False
    _need(props is not None, "coping fabrication metadata missing: " + child.Name)
    config = record["configuration"]
    length, width, height = box.Max.X - box.Min.X, box.Max.Y - box.Min.Y, box.Max.Z - box.Min.Z
    thickness, density = config["COPING_MM"], config["ALU_DENSITY"]
    _need(abs(width - config["COPING_W"]) < 1e-7 and abs(height - 50) < 1e-7,
          "coping envelope differs from source-bound fabrication inputs")
    mass = length * width * thickness * density / 1e9
    expected = {"PhysicalThicknessMM": thickness, "FabricationLengthMM": length,
                "FabricationWidthMM": width, "MaterialDensityKGPerM3": density,
                "PhysicalMassKG": mass, "EnvelopeHeightMM": height}
    for name, value in expected.items():
        actual = props.get(name)
        _need(type(actual) in (float, int) and math.isfinite(actual)
              and math.isclose(actual, value, rel_tol=0.0, abs_tol=1e-9),
              "coping fabrication value differs from source: " + name + " " + child.Name)
    _need(props.get("QuantityBasis") == "FLAT_SHEET_NOT_ENVELOPE_VOLUME"
          and props.get("Assumption") == "FICTIONAL_DEMONSTRATION_INPUTS"
          and props.get("SourceModelSHA256") == record["source_sha256"],
          "coping fabrication basis/provenance missing: " + child.Name)
    _need("%.3f" % mass == source_props["coping_alu_kg"], "coping source rounded quantity differs from fabrication mass")
    return True


def check_part_geometry(f, part, source_part):
    """以 IFC 几何引擎解析局部 Body；与真实 Rhino Brep 顶点、体积比较。"""
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", False)
    shape = ifcopenshell.geom.create_shape(settings, part)
    scale = 1.0 / unit.calculate_unit_scale(f)
    vertices = [tuple(shape.geometry.verts[i + j] * scale for j in range(3))
                for i in range(0, len(shape.geometry.verts), 3)]
    expected = {(round(v.Location.X, 5), round(v.Location.Y, 5), round(v.Location.Z, 5))
                for v in source_part.Geometry.Vertices}
    actual = {tuple(round(value, 5) for value in vertex) for vertex in vertices}
    _need(actual == expected, "part geometry vertices differ from Rhino: " + part.Name)
    faces = shape.geometry.faces
    volume = 0.0
    edges = Counter()
    for i in range(0, len(faces), 3):
        a, b, c = (vertices[faces[i + j]] for j in range(3))
        volume += (a[0] * (b[1] * c[2] - b[2] * c[1])
                   - a[1] * (b[0] * c[2] - b[2] * c[0])
                   + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6.0
        for j, k in ((0, 1), (1, 2), (2, 0)):
            edges[tuple(sorted((faces[i + j], faces[i + k])))] += 1
    box = source_part.Geometry.GetBoundingBox()
    expected_volume = (box.Max.X - box.Min.X) * (box.Max.Y - box.Min.Y) * (box.Max.Z - box.Min.Z)
    _need(all(count == 2 for count in edges.values()), "part geometry is not a closed mesh: " + part.Name)
    _need(math.isclose(abs(volume), expected_volume, rel_tol=1e-7, abs_tol=1e-3),
          "part geometry volume differs from Rhino: " + part.Name)


def check_tasks(f, source_panels):
    schedules = f.by_type("IfcWorkSchedule")
    _need(len(schedules) == 1 and schedules[0].PredefinedType == "PLANNED", "one planned work schedule required")
    schedule = schedules[0]
    _need(any(schedule in rel.RelatedDefinitions for rel in f.by_type("IfcRelDeclares")
              if rel.RelatingContext.is_a("IfcProject")), "work schedule missing project declaration")
    first = min(obj.Attributes.GetUserString("delivery_date") for obj in source_panels.values())
    last = max(obj.Attributes.GetUserString("install_date") for obj in source_panels.values())
    _need(schedule.StartTime == first + "T00:00:00", "schedule start differs from source")
    _need(schedule.FinishTime == (date.fromisoformat(last) + timedelta(days=1)).isoformat() + "T00:00:00",
          "schedule finish differs from source day window")
    tasks = f.by_type("IfcTask")
    task_ids = [task.Identification for task in tasks]
    _need(len(task_ids) == len(set(task_ids)), "duplicate task identification")
    by_id = dict(zip(task_ids, tasks))
    expected_outputs = defaultdict(set)
    expected_dates, expected_kind = {}, {}
    delivery_data = {}
    for pid, obj in source_panels.items():
        attrs = obj.Attributes
        installation, delivery = "INSTALL:" + pid, "DELIVERY:" + attrs.GetUserString("stillage")
        for ident, key, kind in ((installation, "install_date", "INSTALL"), (delivery, "delivery_date", "DELIVERY")):
            event = attrs.GetUserString(key)
            _need(ident not in expected_dates or expected_dates[ident] == event, "inconsistent source task date")
            expected_outputs[ident].add(pid)
            expected_dates[ident], expected_kind[ident] = event, kind
        delivery_data[delivery] = (attrs.GetUserString("stillage"), attrs.GetUserString("truck"))
    _need(set(by_id) == set(expected_dates), "task identity set differs from source panels/stillages")
    assigned = [task for rel in f.by_type("IfcRelAssignsToControl") if rel.RelatingControl == schedule
                for task in rel.RelatedObjects]
    _need(Counter(task.id() for task in assigned) == Counter(task.id() for task in tasks),
          "task missing or duplicated in work schedule")
    outputs = defaultdict(list)
    for rel in f.by_type("IfcRelAssignsToProduct"):
        for task in rel.RelatedObjects:
            if task.is_a("IfcTask"):
                _need(rel.RelatingProduct.is_a("IfcPlate"), "task output must be original parent plate")
                outputs[task.Identification].append(rel.RelatingProduct.Name)
    for ident, task in by_id.items():
        kind, event = expected_kind[ident], date.fromisoformat(expected_dates[ident])
        _need(Counter(outputs[ident]) == Counter(expected_outputs[ident]), "task output association differs: " + ident)
        _need(task.PredefinedType == ("CONSTRUCTION" if kind == "INSTALL" else "USERDEFINED"), "wrong task type: " + ident)
        _need(kind != "DELIVERY" or task.ObjectType == "DELIVERY", "missing delivery ObjectType")
        time = task.TaskTime
        _need(time is not None, "task time missing: " + ident)
        _need(time.ScheduleStart == event.isoformat() + "T00:00:00"
              and time.ScheduleFinish == (event + timedelta(days=1)).isoformat() + "T00:00:00"
              and time.ScheduleDuration == "P1D" and time.DurationType == "ELAPSEDTIME", "task day window differs: " + ident)
        _need(time.ActualStart is None and time.ActualFinish is None and time.ActualDuration is None,
              "unobserved actual task timing must not be invented")
        ps = element.get_pset(task, "FacadeBIM_Task")
        _need(ps and (ps["TimeGranularity"], ps["Source"], ps["EventDate"], ps["EventKind"])
              == ("DAY", "Rhino UserText", event.isoformat(), kind), "task provenance missing: " + ident)
        if kind == "DELIVERY":
            delivery = element.get_pset(task, "FacadeBIM_Delivery")
            _need(delivery and (delivery["Stillage"], delivery["Truck"]) == delivery_data[ident], "delivery logistics differ: " + ident)
    return len(tasks)


def verify(f, source, geometry=True, config_record=None):
    _need(source is not None, "source Rhino model unreadable")
    config_record = config_record or source_configuration(ROOT / "model/facade_bim.3dm")
    source_panels = {obj.Attributes.GetUserString("pid"): obj for obj in source.Objects
                     if isinstance(obj.Geometry, rhino3dm.InstanceReference) and obj.Attributes.GetUserString("pid")}
    source_parts = _source_parts(source)
    definitions = {definition.Id: definition.Name for definition in source.InstanceDefinitions}
    plates = {plate.Name: plate for plate in f.by_type("IfcPlate")}
    _need(len(plates) == len(f.by_type("IfcPlate")) and set(plates) == set(source_panels), "parent panel identity set differs")
    parts_checked, geometry_checked, coping_checked = 0, set(), 0
    all_children = []
    for pid, source_panel in source_panels.items():
        plate = plates[pid]
        expected_guid = ifcopenshell.guid.compress(uuid.uuid5(NAMESPACE, "IfcPlate|" + pid).hex)
        _need(plate.GlobalId == expected_guid, "original plate GlobalId changed: " + pid)
        _need(plate.Representation is None, "parent Body duplicates aggregated parts: " + pid)
        _need(element.get_material(plate) is None, "material belongs on real parts, not composite plate")
        raw = element.get_pset(plate, "FacadeBIM_RhinoUserText") or {}
        source_props = dict(source_panel.Attributes.GetUserStrings())
        _need({key: value for key, value in raw.items() if key != "id"} == source_props,
              "original Rhino UserText changed: " + pid)
        expected_panel = {"PanelID": pid, "Elevation": source_props["elev"], "Level": source_props["level"],
            "Column": int(source_props["col"]), "PanelType": source_props["type"], "TypeName": source_props["type_name"],
            "InstallSequence": int(source_props["seq"]), "InstallDate": source_props["install_date"],
            "Stillage": source_props["stillage"], "Truck": source_props["truck"], "DeliveryDate": source_props["delivery_date"]}
        canonical = element.get_pset(plate, "FacadeBIM_Panel") or {}
        _need({key: value for key, value in canonical.items() if key != "id"} == expected_panel,
              "canonical panel data differs from Rhino: " + pid)
        source_id = element.get_pset(plate, "FacadeBIM_Source") or {}
        _need(source_id.get("RhinoObjectID") == str(source_panel.Attributes.Id), "Rhino source identity missing")
        _close_matrix(placement.get_local_placement(plate.ObjectPlacement), _rhino_matrix(source_panel.Geometry.Xform),
                      "panel placement differs from actual Rhino transform: " + pid)
        definition_id = source_panel.Geometry.ParentIdefId
        definition = definitions[definition_id]
        _need(element.get_type(plate).Name == definition, "plate type differs from Rhino block: " + pid)
        children = [child for rel in plate.IsDecomposedBy for child in rel.RelatedObjects]
        _need(len(children) == len(source_parts[definition_id]), "part count differs from Rhino block: " + pid)
        by_index = {}
        for child in children:
            _need(child.is_a("IfcBuildingElementPart"), "unexpected child class: " + pid)
            ps = element.get_pset(child, "FacadeBIM_Part") or {}
            index = ps.get("PartIndex")
            _need(index not in by_index, "duplicate part index: " + pid)
            by_index[index] = child
        _need(set(by_index) == set(range(1, len(source_parts[definition_id]) + 1)), "part index set differs: " + pid)
        for index, original in enumerate(source_parts[definition_id], 1):
            child = by_index[index]
            ps = element.get_pset(child, "FacadeBIM_Part")
            _need((ps["PanelID"], ps["RhinoObjectID"], ps["RhinoDefinition"], ps["PartKind"])
                  == (pid, str(original.Attributes.Id), definition, original.Attributes.Name), "part provenance differs: " + child.Name)
            _need(child.ObjectType == original.Attributes.Name, "part kind differs: " + child.Name)
            coping_checked += int(check_coping_semantics(child, original, source_props, config_record))
            expected_material = _material(source, original)
            material = element.get_material(child)
            _need(material is not None and material.is_a("IfcMaterial") and material.Name == expected_material.Name
                  and ps["MaterialName"] == expected_material.Name, "part material differs from Rhino: " + child.Name)
            _close_matrix(placement.get_local_placement(child.ObjectPlacement), _rhino_matrix(source_panel.Geometry.Xform),
                          "part placement differs from Rhino block: " + child.Name)
            reps = child.Representation.Representations if child.Representation else []
            _need(len(reps) == 1 and reps[0].RepresentationIdentifier == "Body" and len(reps[0].Items) == 1,
                  "part Body missing: " + child.Name)
            mapped = reps[0].Items[0]
            _need(mapped.is_a("IfcMappedItem"), "part must map actual shared block geometry")
            solid = mapped.MappingSource.MappedRepresentation.Items[0]
            style = solid.StyledByItem[0].Styles[0].Styles[0]
            rgb = (style.SurfaceColour.Red, style.SurfaceColour.Green, style.SurfaceColour.Blue)
            _need(all(math.isclose(actual, expected / 255.0, abs_tol=1e-9) for actual, expected in zip(rgb, expected_material.DiffuseColor[:3]))
                  and math.isclose(style.Transparency or 0.0, expected_material.Transparency, abs_tol=1e-9),
                  "part appearance differs from Rhino material: " + child.Name)
            _close_matrix(placement.get_cartesiantransformationoperator3d(mapped.MappingTarget),
                          [[float(i == j) for j in range(4)] for i in range(4)], "part mapping changes source shape")
            geometry_key = (mapped.MappingSource.id(), str(original.Attributes.Id))
            if geometry and geometry_key not in geometry_checked:
                check_part_geometry(f, child, original)
                geometry_checked.add(geometry_key)
            parts_checked += 1
            all_children.append(child.id())
    _need(Counter(all_children) == Counter(part.id() for part in f.by_type("IfcBuildingElementPart")),
          "orphan or multiply aggregated building element part")
    _need(coping_checked == sum(obj.Attributes.GetUserString("type") == "U5" for obj in source_panels.values()),
          "one actual coping per roof panel is required")
    tasks = check_tasks(f, source_panels)
    return {"panels": len(plates), "parts": parts_checked, "shared_part_geometries": len(geometry_checked),
            "materials": len(f.by_type("IfcMaterial")), "tasks": tasks, "coping_fabrication_parts": coping_checked}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ifc", type=Path, default=ROOT / "model" / "facade_bim.ifc")
    parser.add_argument("--source", type=Path, default=ROOT / "model" / "facade_bim.3dm")
    args = parser.parse_args()
    result = verify(ifcopenshell.open(str(args.ifc)), rhino3dm.File3dm.Read(str(args.source)),
                    config_record=source_configuration(args.source))
    print("PASS IFC ↔ Rhino: " + ", ".join("%s=%s" % item for item in result.items()))


if __name__ == "__main__":
    main()

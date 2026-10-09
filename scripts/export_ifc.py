"""从 Rhino .3dm 导出 IFC4：真实块零件、材料，以及日粒度交付/安装任务。

保留父板 IfcPlate 的稳定编号、GlobalId、原 UserText 和工程量。父板聚合
IfcBuildingElementPart，Body 与材料只放在子件，避免父子几何重复计量。
每个真实块 Brep 对应一个子件，几何按块零件共享 RepresentationMap。
本模型的 Brep 都是轴对齐长方体；遇到其它形状明确拒绝，不用包围盒冒充。
中空玻璃仍是 Rhino 实际建出的单个整体，不虚构玻璃片/空气层或五金。

TaskTime 是源日期所对应的 [00:00, 次日00:00) 本地日窗，P1D 表示日期
分辨率，不是单板工时。安装一板一任务，到场一运输架一任务；两类任务的
输出均用 IfcRelAssignsToProduct 关联原父板。源模型未给出分钟级顺序。

    python scripts/export_ifc.py
    python scripts/export_ifc.py --check
"""
from collections import defaultdict
from datetime import date, timedelta
import math
import os
import sys
import tempfile
import uuid

import ifcopenshell
import ifcopenshell.api
import ifcopenshell.guid
import rhino3dm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from facade.source_integrity import read_source_record
MODEL_3DM = os.path.join(ROOT, "model", "facade_bim.3dm")
MODEL_IFC = os.path.join(ROOT, "model", "facade_bim.ifc")
NS = uuid.UUID("5b1f7a1e-2f0c-4b9e-9d1a-6f0a3c2e7b10")
TIMESTAMP = "2026-09-23T00:00:00"
KEYS = ["pid", "elev", "level", "col", "type", "type_name", "w_mm", "h_mm", "origin_x", "origin_y", "origin_z",
        "rot", "vision_igu_m2", "spandrel_m2", "louver_m2", "door_glass_m2", "frame_alu_kg", "backpan_alu_kg",
        "coping_alu_kg", "insulation_m2", "weight_kg", "seq", "install_date", "stillage", "truck", "delivery_date"]


def gid(name):
    return ifcopenshell.guid.compress(uuid.uuid5(NS, name).hex)


def read_3dm(path=MODEL_3DM):
    """兼容原入口：[(全部 UserText, 平移, 绕 Z 角度, 块定义名)]。"""
    model = rhino3dm.File3dm.Read(path)
    if model is None:
        raise ValueError("读不了 %s" % path)
    return _instances(model)


def _instances(model):
    names = {d.Id: d.Name for d in model.InstanceDefinitions}
    out, seen = [], set()
    for obj in model.Objects:
        g, a = obj.Geometry, obj.Attributes
        if not isinstance(g, rhino3dm.InstanceReference) or not a.GetUserString("pid"):
            continue
        props = dict(a.GetUserStrings())
        pid = props["pid"]
        if pid in seen or any(not props.get(key) for key in KEYS):
            raise ValueError("板块编号重复或原始属性不完整：" + pid)
        seen.add(pid)
        xf = g.Xform
        angle = math.degrees(math.atan2(xf.M10, xf.M00)) % 360
        out.append((props, (xf.M03, xf.M13, xf.M23), angle, names[g.ParentIdefId]))
    if not out:
        raise ValueError(".3dm 中没有幕墙板块实例")
    return out


def _box_bounds(geometry):
    """证明这是实际的轴对齐 box Brep 后才使用其六个边界坐标。"""
    if not isinstance(geometry, rhino3dm.Brep) or not geometry.IsSolid or not geometry.IsValid:
        raise ValueError("仅支持源模型中的实体 box Brep，不支持近似导出")
    bb = geometry.GetBoundingBox()
    lo = (bb.Min.X, bb.Min.Y, bb.Min.Z)
    hi = (bb.Max.X, bb.Max.Y, bb.Max.Z)
    if not all(math.isfinite(v) for v in lo + hi) or any(b <= a for a, b in zip(lo, hi)):
        raise ValueError("源 Brep 的尺寸无效")
    corners = {(round(x, 6), round(y, 6), round(z, 6))
               for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])}
    vertices = {(round(v.Location.X, 6), round(v.Location.Y, 6), round(v.Location.Z, 6))
                for v in geometry.Vertices}
    if (len(geometry.Vertices) != 8 or len(geometry.Faces) != 6 or len(geometry.Edges) != 12
            or vertices != corners or not all(face.IsPlanar() for face in geometry.Faces)
            or not all(edge.IsLinear() for edge in geometry.Edges)):
        raise ValueError("源 Brep 不是轴对齐长方体；不能用包围盒替代真实几何")
    return lo + hi


def _material_info(model, obj):
    attrs = obj.Attributes
    if attrs.MaterialSource != rhino3dm.ObjectMaterialSource.MaterialFromObject:
        raise ValueError("源零件未直接指定材料：" + str(attrs.Id))
    if not 0 <= attrs.MaterialIndex < len(model.Materials):
        raise ValueError("源零件材料索引无效：" + str(attrs.Id))
    material = model.Materials[attrs.MaterialIndex]
    if not material.Name:
        raise ValueError("源材料缺少名称")
    return (material.Name, tuple(material.DiffuseColor[:3]), float(material.Transparency))


def _source(model):
    if model.Settings.ModelUnitSystem != rhino3dm.UnitSystem.Millimeters:
        raise ValueError("源模型必须使用毫米；不隐式缩放")
    objects = {obj.Attributes.Id: obj for obj in model.Objects}
    definitions = {}
    for definition in model.InstanceDefinitions:
        definitions[definition.Name] = [
            {"index": i, "kind": objects[oid].Attributes.Name, "rhino_id": str(oid),
             "bounds": _box_bounds(objects[oid].Geometry), "material": _material_info(model, objects[oid])}
            for i, oid in enumerate(definition.GetObjectIds(), 1)]
    instances = {obj.Attributes.GetUserString("pid"): obj for obj in model.Objects
                 if isinstance(obj.Geometry, rhino3dm.InstanceReference) and obj.Attributes.GetUserString("pid")}
    posts, slabs = [], []
    for obj in model.Objects:
        layer = model.Layers.FindIndex(obj.Attributes.LayerIndex).FullPath
        if layer not in ("幕墙::转角立柱", "结构::楼板"):
            continue
        item = {"bounds": _box_bounds(obj.Geometry), "material": _material_info(model, obj),
                "rhino_id": str(obj.Attributes.Id), "name": obj.Attributes.Name}
        (posts if layer == "幕墙::转角立柱" else slabs).append(item)
    return definitions, instances, posts, slabs


def _is_coping(props, part):
    return (props["type"] == "U5" and part["kind"] == "alu"
            and abs(part["bounds"][2] - float(props["h_mm"])) < 1e-7)


def _coping_properties(props, part, source_record):
    """Physical flat-sheet estimate; the source Body remains a 50mm envelope."""
    bounds, config = part["bounds"], source_record["configuration"]
    length, width, envelope = (bounds[i + 3] - bounds[i] for i in range(3))
    thickness, density = float(config["COPING_MM"]), float(config["ALU_DENSITY"])
    if not all(math.isfinite(value) and value > 0 for value in (length, width, envelope, thickness, density)):
        raise ValueError("invalid source-bound coping fabrication inputs")
    if abs(width - float(config["COPING_W"])) > 1e-7 or abs(envelope - 50) > 1e-7:
        raise ValueError("source coping envelope differs from its configuration baseline")
    mass = length * width * thickness * density / 1e9
    if "%.3f" % mass != props["coping_alu_kg"]:
        raise ValueError("source coping quantity differs from actual source span")
    return {"PhysicalThicknessMM": thickness, "FabricationLengthMM": length,
            "FabricationWidthMM": width, "MaterialDensityKGPerM3": density,
            "PhysicalMassKG": mass, "EnvelopeHeightMM": envelope,
            "QuantityBasis": "FLAT_SHEET_NOT_ENVELOPE_VOLUME",
            "Assumption": "FICTIONAL_DEMONSTRATION_INPUTS",
            "SourceModelSHA256": source_record["source_sha256"]}


def _axis(f, origin=(0.0, 0.0, 0.0), axis=None, reference=None):
    return f.createIfcAxis2Placement3D(f.createIfcCartesianPoint(tuple(float(v) for v in origin)),
                                     f.createIfcDirection(axis) if axis else None,
                                     f.createIfcDirection(reference) if reference else None)


def _placement(f, origin=(0.0, 0.0, 0.0), relative_to=None):
    return f.createIfcLocalPlacement(relative_to, _axis(f, origin))


def _instance_placement(f, transform):
    if not all(math.isfinite(getattr(transform, "M%d%d" % (i, j))) for i in range(4) for j in range(4)):
        raise ValueError("块实例变换含非有限坐标")
    cols = [tuple(getattr(transform, "M%d%d" % (i, j)) for i in range(3)) for j in range(3)]
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))
    cross = (cols[0][1] * cols[1][2] - cols[0][2] * cols[1][1],
             cols[0][2] * cols[1][0] - cols[0][0] * cols[1][2],
             cols[0][0] * cols[1][1] - cols[0][1] * cols[1][0])
    if (any(abs(dot(a, b) - (1.0 if i == j else 0.0)) > 1e-9
            for i, a in enumerate(cols) for j, b in enumerate(cols))
            or abs(dot(cross, cols[2]) - 1.0) > 1e-9
            or any(abs(getattr(transform, "M3%d" % j) - (1.0 if j == 3 else 0.0)) > 1e-9 for j in range(4))):
        raise ValueError("块实例含缩放、镜像或非刚性变换，不能丢失变换后导出")
    origin = (transform.M03, transform.M13, transform.M23)
    return f.createIfcLocalPlacement(None, _axis(f, origin, cols[2], cols[0]))


def _box_solid(f, bounds):
    x0, y0, z0, x1, y1, z1 = bounds
    profile = f.createIfcRectangleProfileDef("AREA", None, f.createIfcAxis2Placement2D(
        f.createIfcCartesianPoint(((x0 + x1) / 2.0, (y0 + y1) / 2.0)), None), x1 - x0, y1 - y0)
    return f.createIfcExtrudedAreaSolid(profile, _axis(f, (0.0, 0.0, z0)),
                                        f.createIfcDirection((0.0, 0.0, 1.0)), z1 - z0)


def _pset(f, product, name, properties):
    run = ifcopenshell.api.run
    pset = run("pset.add_pset", f, product=product, name=name)
    run("pset.edit_pset", f, pset=pset, properties=properties)


def _tasks(f, project, plates, props_by_id):
    """只表达已有计划日期，不声称源模型提供了分钟级工时或实测进度。"""
    groups = defaultdict(list)
    for pid, props in props_by_id.items():
        delivery = date.fromisoformat(props["delivery_date"])
        install = date.fromisoformat(props["install_date"])
        if delivery > install:
            raise ValueError("到场晚于安装：" + pid)
        groups[props["stillage"]].append(pid)
    first = min(props["delivery_date"] for props in props_by_id.values())
    last = max(props["install_date"] for props in props_by_id.values())
    schedule = f.create_entity("IfcWorkSchedule", GlobalId=gid("schedule"), Name="幕墙交付与安装（日粒度）",
        Description="Source: Rhino UserText. Local date windows [00:00,next 00:00); P1D is date resolution, not labor duration.",
        Identification="FACADE-INSTALL-DELIVERY", CreationDate=TIMESTAMP,
        StartTime=first + "T00:00:00", FinishTime=(date.fromisoformat(last) + timedelta(days=1)).isoformat() + "T00:00:00",
        PredefinedType="PLANNED")
    f.create_entity("IfcRelDeclares", GlobalId=gid("schedule context"), RelatingContext=project,
                    RelatedDefinitions=[schedule])
    tasks, outputs = [], defaultdict(list)

    def task(identification, event_date, kind):
        event = date.fromisoformat(event_date)
        item = f.create_entity("IfcTask", GlobalId=gid(identification), Name=identification,
            Identification=identification, ObjectType=kind if kind == "DELIVERY" else None,
            Description="Date-resolution planning window; actual start/finish and labor duration are not modelled.",
            IsMilestone=False, PredefinedType="CONSTRUCTION" if kind == "INSTALL" else "USERDEFINED")
        item.TaskTime = f.create_entity("IfcTaskTime", Name=identification, DataOrigin="USERDEFINED",
            UserDefinedDataOrigin="Rhino UserText planned date; day window, not actual labor duration",
            DurationType="ELAPSEDTIME", ScheduleDuration="P1D", ScheduleStart=event.isoformat() + "T00:00:00",
            ScheduleFinish=(event + timedelta(days=1)).isoformat() + "T00:00:00")
        _pset(f, item, "FacadeBIM_Task", {"TimeGranularity": "DAY", "Source": "Rhino UserText",
            "EventDate": event.isoformat(), "EventKind": kind})
        tasks.append(item)
        return item

    for sid, pids in sorted(groups.items()):
        dates = {props_by_id[pid]["delivery_date"] for pid in pids}
        trucks = {props_by_id[pid]["truck"] for pid in pids}
        if len(dates) != 1 or len(trucks) != 1:
            raise ValueError("同架到场日或车号不一致：" + sid)
        delivery = task("DELIVERY:" + sid, dates.pop(), "DELIVERY")
        _pset(f, delivery, "FacadeBIM_Delivery", {"Stillage": sid, "Truck": trucks.pop()})
        for pid in pids:
            outputs[pid].append(delivery)
    for pid in sorted(props_by_id, key=lambda value: int(props_by_id[value]["seq"])):
        outputs[pid].append(task("INSTALL:" + pid, props_by_id[pid]["install_date"], "INSTALL"))
    f.create_entity("IfcRelAssignsToControl", GlobalId=gid("tasks in schedule"), RelatedObjects=tasks,
                    RelatingControl=schedule)
    for pid in sorted(outputs):
        f.create_entity("IfcRelAssignsToProduct", GlobalId=gid("task output " + pid),
                        RelatedObjects=outputs[pid], RelatingProduct=plates[pid])


def build_ifc(instances, model=None, source_record=None):
    run = ifcopenshell.api.run
    model = model or rhino3dm.File3dm.Read(MODEL_3DM)
    source_record = source_record or read_source_record(MODEL_3DM)
    definitions, source_instances, posts, slabs = _source(model)
    f = ifcopenshell.file(schema="IFC4")
    f.header.file_name.name = "facade_bim.ifc"
    f.header.file_name.time_stamp = TIMESTAMP
    f.header.file_name.preprocessor_version = "IfcOpenShell %s" % ifcopenshell.version
    f.header.file_name.originating_system = "facade-bim/scripts/export_ifc.py"
    project = run("root.create_entity", f, ifc_class="IfcProject", name="单元式幕墙 BIM（虚构示例建筑）")
    units = run("unit.assign_unit", f)
    # GrossWeight 源值为 kg；显式声明质量单位，不能只靠属性名猜单位。
    units.Units = list(units.Units) + [f.createIfcSIUnit(None, "MASSUNIT", "KILO", "GRAM")]
    model_ctx = run("context.add_context", f, context_type="Model")
    body = run("context.add_context", f, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=model_ctx)
    site = run("root.create_entity", f, ifc_class="IfcSite", name="场地")
    building = run("root.create_entity", f, ifc_class="IfcBuilding", name="示例办公楼")
    run("aggregate.assign_object", f, relating_object=project, products=[site])
    run("aggregate.assign_object", f, relating_object=site, products=[building])
    storeys = {}
    for slab in slabs:
        level = slab["name"].removeprefix("楼板 ")
        storey = run("root.create_entity", f, ifc_class="IfcBuildingStorey", name=level)
        storey.Elevation = slab["bounds"][5]
        storeys[level] = storey
    run("aggregate.assign_object", f, relating_object=building, products=list(storeys.values()))

    material_infos = {part["material"] for parts in definitions.values() for part in parts}
    material_infos.update(item["material"] for item in posts + slabs)
    materials, styles, material_products = {}, {}, defaultdict(list)
    for info in sorted(material_infos):
        name, rgb, transparency = info
        materials[info] = f.create_entity("IfcMaterial", Name=name, Description="Rhino material; no unmodelled layer split")
        colour = f.createIfcColourRgb(name, *(value / 255.0 for value in rgb))
        rendering = f.create_entity("IfcSurfaceStyleRendering", SurfaceColour=colour,
                                    Transparency=transparency, ReflectanceMethod="NOTDEFINED")
        styles[info] = f.createIfcSurfaceStyle(name, "BOTH", [rendering])

    types, part_maps = {}, {}
    for name in sorted({props["type"] for props, _, _, _ in instances}):
        source_parts = definitions[name]
        plate_type = run("root.create_entity", f, ifc_class="IfcPlateType", name=name, predefined_type="CURTAIN_PANEL")
        plate_type.Description = "Rhino block %s: %d actual solid parts; parent occurrence geometry is decomposed" % (name, len(source_parts))
        solids = []
        for part in source_parts:
            solid = _box_solid(f, part["bounds"])
            f.createIfcStyledItem(solid, [styles[part["material"]]], part["kind"])
            solids.append(solid)
            representation = f.createIfcShapeRepresentation(body, "Body", "SweptSolid", [solid])
            part_maps[(name, part["index"])] = f.createIfcRepresentationMap(_axis(f), representation)
        representation = f.createIfcShapeRepresentation(body, "Body", "SweptSolid", solids)
        plate_type.RepresentationMaps = [f.createIfcRepresentationMap(_axis(f), representation)]
        types[name] = plate_type

    cws = {}
    for level in storeys:
        for elevation in dict.fromkeys(props["elev"] for props, _, _, _ in instances):
            cw = run("root.create_entity", f, ifc_class="IfcCurtainWall", name="%s-%s" % (elevation, level), predefined_type="USERDEFINED")
            cw.ObjectType = "单元式幕墙"
            run("spatial.assign_container", f, relating_structure=storeys[level], products=[cw])
            cws[(level, elevation)] = cw
    identity = f.createIfcCartesianTransformationOperator3D(None, None, f.createIfcCartesianPoint((0.0, 0.0, 0.0)), None, None)
    by_cw, by_type, plates, props_by_id = defaultdict(list), defaultdict(list), {}, {}
    for props, _, _, definition in sorted(instances, key=lambda item: int(item[0]["seq"])):
        pid, ptype = props["pid"], props["type"]
        if definition != ptype:
            raise ValueError("块定义与类型属性不一致：" + pid)
        plate = run("root.create_entity", f, ifc_class="IfcPlate", name=pid, predefined_type="CURTAIN_PANEL")
        _pset(f, plate, "Pset_PlateCommon", {"Reference": ptype, "IsExternal": True, "LoadBearing": False})
        _pset(f, plate, "FacadeBIM_Panel", {"PanelID": pid, "Elevation": props["elev"], "Level": props["level"],
            "Column": int(props["col"]), "PanelType": ptype, "TypeName": props["type_name"],
            "InstallSequence": int(props["seq"]), "InstallDate": props["install_date"], "Stillage": props["stillage"],
            "Truck": props["truck"], "DeliveryDate": props["delivery_date"]})
        _pset(f, plate, "FacadeBIM_RhinoUserText", props)
        source_obj = source_instances[pid]
        _pset(f, plate, "FacadeBIM_Source", {"RhinoObjectID": str(source_obj.Attributes.Id), "RhinoDefinition": definition,
            "GeometryScope": "Body only on aggregated actual Rhino parts", "QuantityBasis": "Original Rhino UserText; geometric solid volume is not estimated fabrication mass"})
        w, h = float(props["w_mm"]), float(props["h_mm"])
        # 原 Qto.Width 是单元主体进深；首个实际块零件为竖框。压顶的局部
        # 外伸属于分件几何，不能在深化时把原主体工程量悄悄改成包络进深。
        main_frame = definitions[ptype][0]["bounds"]
        thickness = main_frame[4] - main_frame[1]
        qto = run("pset.add_qto", f, product=plate, name="Qto_PlateBaseQuantities")
        run("pset.edit_qto", f, qto=qto, properties={"Width": thickness, "Perimeter": 2.0 * (w + h),
            "GrossArea": w * h / 1e6, "GrossWeight": float(props["weight_kg"])})
        by_cw[(props["level"], props["elev"])].append(plate)
        by_type[ptype].append(plate)
        plates[pid], props_by_id[pid] = plate, props
    for key, products in by_cw.items():
        run("aggregate.assign_object", f, relating_object=cws[key], products=products)
    for ptype, products in by_type.items():
        run("type.assign_type", f, related_objects=products, relating_type=types[ptype], should_map_representations=False)
    for pid, plate in plates.items():
        plate.ObjectPlacement = _instance_placement(f, source_instances[pid].Geometry.Xform)
        parts = []
        for part in definitions[props_by_id[pid]["type"]]:
            name = "%s/%02d %s" % (pid, part["index"], part["kind"])
            child = run("root.create_entity", f, ifc_class="IfcBuildingElementPart", name=name, predefined_type="USERDEFINED")
            child.ObjectType = part["kind"]
            child.ObjectPlacement = _placement(f, relative_to=plate.ObjectPlacement)
            mapped = f.createIfcMappedItem(part_maps[(props_by_id[pid]["type"], part["index"])], identity)
            child.Representation = f.createIfcProductDefinitionShape(None, None,
                [f.createIfcShapeRepresentation(body, "Body", "MappedRepresentation", [mapped])])
            _pset(f, child, "FacadeBIM_Part", {"PanelID": pid, "PartIndex": part["index"], "PartKind": part["kind"],
                "RhinoObjectID": part["rhino_id"], "RhinoDefinition": props_by_id[pid]["type"], "MaterialName": part["material"][0],
                "PartRole": "ROOF_COPING" if _is_coping(props_by_id[pid], part) else "RHINO_BLOCK_PART",
                "GeometryRepresentation": "CONSTRUCTION_ENVELOPE" if _is_coping(props_by_id[pid], part) else "SOURCE_GEOMETRY"})
            if _is_coping(props_by_id[pid], part):
                _pset(f, child, "FacadeBIM_Coping", _coping_properties(props_by_id[pid], part, source_record))
            material_products[part["material"]].append(child)
            parts.append(child)
        f.create_entity("IfcRelAggregates", GlobalId=gid("parts " + pid), RelatingObject=plate, RelatedObjects=parts)

    for i, item in enumerate(posts, 1):
        post = run("root.create_entity", f, ifc_class="IfcMember", name="转角立柱 %d" % i, predefined_type="MULLION")
        post.Representation = f.createIfcProductDefinitionShape(None, None,
            [f.createIfcShapeRepresentation(body, "Body", "SweptSolid", [_box_solid(f, item["bounds"])])])
        run("spatial.assign_container", f, relating_structure=building, products=[post])
        post.ObjectPlacement = _placement(f)
        material_products[item["material"]].append(post)
    for item in slabs:
        level = item["name"].removeprefix("楼板 ")
        slab = run("root.create_entity", f, ifc_class="IfcSlab", name=item["name"], predefined_type="FLOOR")
        slab.Representation = f.createIfcProductDefinitionShape(None, None,
            [f.createIfcShapeRepresentation(body, "Body", "SweptSolid", [_box_solid(f, item["bounds"])])])
        run("spatial.assign_container", f, relating_structure=storeys[level], products=[slab])
        slab.ObjectPlacement = _placement(f)
        material_products[item["material"]].append(slab)
    for info, products in material_products.items():
        f.create_entity("IfcRelAssociatesMaterial", GlobalId=gid("material " + str(info)),
                        RelatedObjects=products, RelatingMaterial=materials[info])
    _tasks(f, project, plates, props_by_id)
    _normalize_sets(f)
    _stable_ids(f)
    return f


def _stable_ids(f):
    """旧父板仍为 gid('IfcPlate|'+pid)；其它实体按名称和关系端点稳定命名。"""
    seen = {}
    for ent in f.by_type("IfcRoot"):
        base = "%s|%s" % (ent.is_a(), ent.Name or "")
        if ent.is_a("IfcRelationship"):
            ends = []
            for attr in ("RelatingObject", "RelatingStructure", "RelatingType", "RelatingPropertyDefinition",
                         "RelatingProduct", "RelatingControl", "RelatingContext", "RelatingMaterial"):
                value = getattr(ent, attr, None)
                if value is not None:
                    ends.append(value.GlobalId if hasattr(value, "GlobalId") else str(value.id()))
            for attr in ("RelatedObjects", "RelatedElements", "RelatedDefinitions"):
                value = getattr(ent, attr, None)
                if value:
                    ends.append(value[0].GlobalId)
            base += "|" + "|".join(ends)
        elif ent.is_a("IfcPropertySetDefinition"):
            owner = ent.DefinesOccurrence[0].RelatedObjects[0] if ent.DefinesOccurrence else None
            base += "|" + (owner.Name if owner is not None else "")
        count = seen.get(base, 0)
        seen[base] = count + 1
        ent.GlobalId = gid(base + ("#%d" % count if count else ""))


SET_ATTRS = {
    "IfcRelAggregates": ["RelatedObjects"], "IfcRelContainedInSpatialStructure": ["RelatedElements"],
    "IfcRelDefinesByType": ["RelatedObjects"], "IfcRelDefinesByProperties": ["RelatedObjects"],
    "IfcRelDeclares": ["RelatedDefinitions"], "IfcRelAssignsToControl": ["RelatedObjects"],
    "IfcRelAssignsToProduct": ["RelatedObjects"], "IfcRelAssociatesMaterial": ["RelatedObjects"],
    "IfcUnitAssignment": ["Units"], "IfcPropertySet": ["HasProperties"],
    "IfcElementQuantity": ["Quantities"], "IfcShapeRepresentation": ["Items"],
}


def _normalize_sets(f):
    for cls, attrs in SET_ATTRS.items():
        for ent in f.by_type(cls, include_subtypes=False):
            for attr in attrs:
                values = getattr(ent, attr, None)
                if values and len(values) > 1:
                    setattr(ent, attr, sorted(values, key=lambda value: value.id()))


def export(path=MODEL_IFC, source_path=MODEL_3DM):
    model = rhino3dm.File3dm.Read(source_path)
    if model is None:
        raise ValueError("读不了 " + source_path)
    instances = _instances(model)
    f = build_ifc(instances, model, read_source_record(source_path))
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(f.to_string())
    return f, instances


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if "--check" in sys.argv:
        with tempfile.TemporaryDirectory(prefix="facade_ifc_") as temporary:
            path = os.path.join(temporary, "facade_bim.ifc")
            export(path)
            with open(path, "rb") as stream:
                actual = stream.read()
            with open(MODEL_IFC, "rb") as stream:
                committed = stream.read()
        if actual != committed:
            sys.exit("MISMATCH: 由 .3dm 重导的 IFC 与已提交的 model/facade_bim.ifc 不一致")
        print("PASS 由 .3dm 重导的 IFC 与已提交文件逐字节一致（%d 字节）" % len(actual))
        return
    f, instances = export()
    print("写出 %s：%d 块板、%d 个真实零件、%d 个任务、%d 个实体、%d 字节" % (
        os.path.relpath(MODEL_IFC, ROOT), len(instances), len(f.by_type("IfcBuildingElementPart")),
        len(f.by_type("IfcTask")), len(list(f)), os.path.getsize(MODEL_IFC)))


if __name__ == "__main__":
    main()

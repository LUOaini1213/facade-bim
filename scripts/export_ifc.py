"""把 Rhino 建出的 .3dm 导出成 IFC4。

数据来源是 model/facade_bim.3dm 本身：块实例的变换给位置和朝向，实例上挂的
UserText 给编号、类型、尺寸、排程——不从参数重算。这样 IFC 证明的是
「Rhino 里建的这个模型能交付成开放格式」，而不是另一套平行的计算。

IFC 结构：
    IfcProject → IfcSite → IfcBuilding → IfcBuildingStorey ×9
    每层每个立面一个 IfcCurtainWall（共 36 个），聚合该段的 IfcPlate
    5 个 IfcPlateType（对应 Rhino 的 5 个块定义），几何用 IfcRepresentationMap
    共享，每块板用 IfcMappedItem 引用——与 Rhino 块的「类型 / 实例」一一对应
    4 根转角立柱为 IfcMember（MULLION），9 块楼板为 IfcSlab
    每块板挂 Pset_PlateCommon、自定义 FacadeBIM_Panel、Qto_PlateBaseQuantities

GlobalId 由编号经 uuid5 推出、文件头时间戳固定，所以同一个 .3dm 每次导出
的 IFC 逐字节相同，CI 能直接比对。

    python scripts/export_ifc.py            # 写 model/facade_bim.ifc
    python scripts/export_ifc.py --check    # 重导一遍，与已提交的 IFC 逐字节比对
"""
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
from facade import config as C                    # noqa: E402
from facade.model import TYPE_NAMES, corner_posts  # noqa: E402

MODEL_3DM = os.path.join(ROOT, "model", "facade_bim.3dm")
MODEL_IFC = os.path.join(ROOT, "model", "facade_bim.ifc")
NS = uuid.UUID("5b1f7a1e-2f0c-4b9e-9d1a-6f0a3c2e7b10")   # 固定命名空间，GlobalId 可复现
TIMESTAMP = "2026-09-23T00:00:00"
KEYS = ["pid", "elev", "level", "col", "type", "type_name", "w_mm", "h_mm", "origin_x", "origin_y", "origin_z",
        "rot", "vision_igu_m2", "spandrel_m2", "louver_m2", "door_glass_m2", "frame_alu_kg", "backpan_alu_kg",
        "coping_alu_kg", "insulation_m2", "weight_kg", "seq", "install_date", "stillage", "truck", "delivery_date"]


def gid(name):
    return ifcopenshell.guid.compress(uuid.uuid5(NS, name).hex)


def read_3dm(path=MODEL_3DM):
    """返回 [(属性字典, 平移 (x,y,z), 旋转角度, 块定义名)]，只取挂了板块编号的实例。"""
    m = rhino3dm.File3dm.Read(path)
    if m is None:
        raise SystemExit("读不了 %s" % path)
    idefs = {d.Id: d.Name for d in m.InstanceDefinitions}
    out = []
    for obj in m.Objects:
        g = obj.Geometry
        if not isinstance(g, rhino3dm.InstanceReference):
            continue
        a = obj.Attributes
        if not a.GetUserString("pid"):
            continue                                   # 型录里的示意实例没有编号，不入 IFC
        props = {k: a.GetUserString(k) for k in KEYS}
        xf = g.Xform
        rot = math.degrees(math.atan2(xf.M10, xf.M00)) % 360
        out.append((props, (xf.M03, xf.M13, xf.M23), int(round(rot)) % 360, idefs[g.ParentIdefId]))
    return out


def _placement(f, origin, rot):
    a = math.radians(rot)
    return f.createIfcLocalPlacement(None, f.createIfcAxis2Placement3D(
        f.createIfcCartesianPoint(tuple(float(v) for v in origin)),
        f.createIfcDirection((0.0, 0.0, 1.0)),
        f.createIfcDirection((round(math.cos(a), 12), round(math.sin(a), 12), 0.0))))


def _box_solid(f, w, d, h):
    """局部坐标 x∈[0,w]、y∈[0,d]、z∈[0,h] 的拉伸实体。"""
    prof = f.createIfcRectangleProfileDef("AREA", None, f.createIfcAxis2Placement2D(
        f.createIfcCartesianPoint((w / 2.0, d / 2.0)), None), float(w), float(d))
    return f.createIfcExtrudedAreaSolid(prof, f.createIfcAxis2Placement3D(
        f.createIfcCartesianPoint((0.0, 0.0, 0.0)), None, None), f.createIfcDirection((0.0, 0.0, 1.0)), float(h))


def build_ifc(instances):
    run = ifcopenshell.api.run
    f = ifcopenshell.file(schema="IFC4")
    f.header.file_name.name = "facade_bim.ifc"
    f.header.file_name.time_stamp = TIMESTAMP
    f.header.file_name.preprocessor_version = "IfcOpenShell %s" % ifcopenshell.version
    f.header.file_name.originating_system = "facade-bim/scripts/export_ifc.py"

    project = run("root.create_entity", f, ifc_class="IfcProject", name="单元式幕墙 BIM（虚构示例建筑）")
    run("unit.assign_unit", f)                                  # 默认：毫米、平方米、立方米
    model_ctx = run("context.add_context", f, context_type="Model")
    body = run("context.add_context", f, context_type="Model", context_identifier="Body",
               target_view="MODEL_VIEW", parent=model_ctx)
    site_e = run("root.create_entity", f, ifc_class="IfcSite", name="场地")
    bldg = run("root.create_entity", f, ifc_class="IfcBuilding", name="示例办公楼")
    run("aggregate.assign_object", f, relating_object=project, products=[site_e])
    run("aggregate.assign_object", f, relating_object=site_e, products=[bldg])

    storeys = {}
    for lvl, z, _ in C.LEVELS:
        st = run("root.create_entity", f, ifc_class="IfcBuildingStorey", name=lvl)
        st.Elevation = float(z)
        storeys[lvl] = st
    run("aggregate.assign_object", f, relating_object=bldg, products=list(storeys.values()))

    # 板块类型：几何放进 RepresentationMap，实例用 MappedItem 引用
    sizes = {}
    for props, _, _, _ in instances:
        sizes.setdefault(props["type"], (int(props["w_mm"]), int(props["h_mm"])))
    types, rmaps = {}, {}
    for t in sorted(sizes):
        w, h = sizes[t]
        pt = run("root.create_entity", f, ifc_class="IfcPlateType", name=t, predefined_type="CURTAIN_PANEL")
        pt.Description = "%s，制作尺寸 %d×%d×%d mm" % (TYPE_NAMES[t], w, h, C.DEPTH)
        rep = f.createIfcShapeRepresentation(body, "Body", "SweptSolid", [_box_solid(f, w, C.DEPTH, h)])
        rmap = f.createIfcRepresentationMap(f.createIfcAxis2Placement3D(
            f.createIfcCartesianPoint((0.0, 0.0, 0.0)), None, None), rep)
        pt.RepresentationMaps = [rmap]
        types[t], rmaps[t] = pt, rmap

    # 每层每立面一个幕墙段
    cws = {}
    for lvl, _, _ in C.LEVELS:
        for elev, _ in C.ELEVATIONS:
            cw = run("root.create_entity", f, ifc_class="IfcCurtainWall", name="%s-%s" % (elev, lvl),
                     predefined_type="USERDEFINED")
            cw.ObjectType = "单元式幕墙"
            run("spatial.assign_container", f, relating_structure=storeys[lvl], products=[cw])
            cws[(lvl, elev)] = cw

    ident = f.createIfcCartesianTransformationOperator3D(None, None, f.createIfcCartesianPoint((0.0, 0.0, 0.0)), None, None)
    by_cw, by_type, placements = {}, {}, []
    for props, origin, rot, idef in sorted(instances, key=lambda x: int(x[0]["seq"])):
        t = props["type"]
        if idef != t:
            raise SystemExit("块定义与属性不一致：%s 用了块 %s，属性写的是 %s" % (props["pid"], idef, t))
        plate = run("root.create_entity", f, ifc_class="IfcPlate", name=props["pid"], predefined_type="CURTAIN_PANEL")
        placements.append((plate, origin, rot))
        mapped = f.createIfcMappedItem(rmaps[t], ident)
        plate.Representation = f.createIfcProductDefinitionShape(None, None, [
            f.createIfcShapeRepresentation(body, "Body", "MappedRepresentation", [mapped])])
        w, h = int(props["w_mm"]), int(props["h_mm"])
        common = run("pset.add_pset", f, product=plate, name="Pset_PlateCommon")
        run("pset.edit_pset", f, pset=common, properties={"Reference": t, "IsExternal": True, "LoadBearing": False})
        own = run("pset.add_pset", f, product=plate, name="FacadeBIM_Panel")
        run("pset.edit_pset", f, pset=own, properties={
            "PanelID": props["pid"], "Elevation": props["elev"], "Level": props["level"],
            "Column": int(props["col"]), "PanelType": t, "TypeName": props["type_name"],
            "InstallSequence": int(props["seq"]), "InstallDate": props["install_date"],
            "Stillage": props["stillage"], "Truck": props["truck"], "DeliveryDate": props["delivery_date"]})
        qto = run("pset.add_qto", f, product=plate, name="Qto_PlateBaseQuantities")
        run("pset.edit_qto", f, qto=qto, properties={
            "Width": float(C.DEPTH), "Perimeter": 2.0 * (w + h), "GrossArea": w * h / 1e6,
            "GrossWeight": float(props["weight_kg"])})
        by_cw.setdefault((props["level"], props["elev"]), []).append(plate)
        by_type.setdefault(t, []).append(plate)
    for key, plates in by_cw.items():
        run("aggregate.assign_object", f, relating_object=cws[key], products=plates)
    for t, plates in by_type.items():
        run("type.assign_type", f, related_objects=plates, relating_type=types[t], should_map_representations=False)
    # 定位最后写：ifcopenshell.api 的 assign_container / assign_object 会把已有定位改写成
    # 相对容器的定位并新建实体，且按集合顺序处理——先挂关系、后写定位，实体编号才确定。
    for plate, origin, rot in placements:
        plate.ObjectPlacement = _placement(f, origin, rot)

    # 转角立柱与楼板（语境构件）
    posts = []
    for i, (x, y, s, top) in enumerate(corner_posts(), 1):
        m = run("root.create_entity", f, ifc_class="IfcMember", name="转角立柱 %d" % i, predefined_type="MULLION")
        m.Representation = f.createIfcProductDefinitionShape(None, None, [
            f.createIfcShapeRepresentation(body, "Body", "SweptSolid", [_box_solid(f, s, s, top)])])
        posts.append((m, (x, y, 0)))
    run("spatial.assign_container", f, relating_structure=bldg, products=[m for m, _ in posts])
    for m, o in posts:
        m.ObjectPlacement = _placement(f, o, 0)
    from facade.model import OUT_X, OUT_Y
    e = C.DEPTH + C.SLAB_GAP
    for lvl, z, _ in C.LEVELS:
        slab = run("root.create_entity", f, ifc_class="IfcSlab", name="楼板 %s" % lvl, predefined_type="FLOOR")
        slab.Representation = f.createIfcProductDefinitionShape(None, None, [
            f.createIfcShapeRepresentation(body, "Body", "SweptSolid", [_box_solid(f, OUT_X - 2 * e, OUT_Y - 2 * e, 250)])])
        run("spatial.assign_container", f, relating_structure=storeys[lvl], products=[slab])
        slab.ObjectPlacement = _placement(f, (e, e, z - 250), 0)

    _normalize_sets(f)
    # 可复现的 GlobalId：按类别 + 名称（关系按其端点）推出
    seen = {}
    for ent in f.by_type("IfcRoot"):
        base = "%s|%s" % (ent.is_a(), ent.Name or "")
        if ent.is_a("IfcRelationship"):
            ends = []
            for attr in ("RelatingObject", "RelatingStructure", "RelatingType", "RelatingPropertyDefinition"):
                v = getattr(ent, attr, None)
                if v is not None:
                    ends.append(v.GlobalId if hasattr(v, "GlobalId") else str(v))
            for attr in ("RelatedObjects", "RelatedElements"):
                v = getattr(ent, attr, None)
                if v:
                    ends.append(v[0].GlobalId)
            base += "|" + "|".join(ends)
        elif ent.is_a("IfcPropertySetDefinition"):
            owner = ent.DefinesOccurrence[0].RelatedObjects[0] if ent.DefinesOccurrence else None
            base += "|" + (owner.Name if owner is not None else "")
        n = seen.get(base, 0)
        seen[base] = n + 1
        ent.GlobalId = gid(base + ("#%d" % n if n else ""))
    return f


# IFC 里这些属性是 SET（无序），ifcopenshell.api 用 Python 集合存，每次运行顺序不同。
# 落盘前按实体序号排好——实体序号由创建顺序决定，是确定的。LIST 属性（点序、表示序）不动。
SET_ATTRS = {
    "IfcRelAggregates": ["RelatedObjects"],
    "IfcRelContainedInSpatialStructure": ["RelatedElements"],
    "IfcRelDefinesByType": ["RelatedObjects"],
    "IfcRelDefinesByProperties": ["RelatedObjects"],
    "IfcRelDeclares": ["RelatedDefinitions"],
    "IfcUnitAssignment": ["Units"],
    "IfcPropertySet": ["HasProperties"],
    "IfcElementQuantity": ["Quantities"],
    "IfcShapeRepresentation": ["Items"],
}


def _normalize_sets(f):
    for cls, attrs in SET_ATTRS.items():
        for ent in f.by_type(cls, include_subtypes=False):
            for a in attrs:
                v = getattr(ent, a, None)
                if v and len(v) > 1:
                    setattr(ent, a, sorted(v, key=lambda e: e.id()))


def export(path=MODEL_IFC):
    inst = read_3dm()
    f = build_ifc(inst)
    # 自己写文件、固定 LF：ifcopenshell 的 f.write() 在 Windows 上写 CRLF，
    # 仓库按 .gitattributes 存 LF，不固定的话 Windows 本机重导会和检出的文件差一个换行符。
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f.to_string())
    return f, inst


def main():
    if "--check" in sys.argv:
        tmp = os.path.join(tempfile.mkdtemp(), "facade_bim.ifc")
        export(tmp)
        a = open(tmp, "rb").read()
        b = open(MODEL_IFC, "rb").read() if os.path.exists(MODEL_IFC) else b""
        if a != b:
            print("MISMATCH: 由 .3dm 重导的 IFC 与已提交的 model/facade_bim.ifc 不一致")
            sys.exit(1)
        print("PASS 由 .3dm 重导的 IFC 与已提交文件逐字节一致（%d 字节）" % len(a))
        return
    f, inst = export()
    print("写出 %s：%d 块板，%d 个实体，%d 字节" % (os.path.relpath(MODEL_IFC, ROOT), len(inst),
                                            len(list(f)), os.path.getsize(MODEL_IFC)))


if __name__ == "__main__":
    main()

#! python3
"""Rhino 8 原生日期回放。打开 facade_bim.3dm 后运行，或由 run_replay.py 批量出图。"""
import json
import math
import os
import shutil
import sys
import time
import traceback
from datetime import date, timedelta

ROOT = os.environ.get("FACADE_BIM_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import System
import System.Drawing as SD
import System.Drawing.Imaging
import Rhino
import Rhino.Geometry as RG
import rhinoscriptsyntax as rs

from facade import site
from facade.replay import snapshot

PREFIX = "施工回放"
KEYS = ("pid", "install_date", "delivery_date", "stillage", "w_mm", "h_mm")


def layer(doc, name, rgb):
    full = PREFIX + "::" + name
    index = doc.Layers.FindByFullPath(full, -1)
    if index >= 0:
        return index
    parent = doc.Layers.FindByFullPath(PREFIX, -1)
    if parent < 0:
        parent = doc.Layers.Add(PREFIX, SD.Color.Black)
    item = Rhino.DocObjects.Layer()
    item.Name = name
    item.ParentLayerId = doc.Layers[parent].Id
    item.Color = SD.Color.FromArgb(*rgb)
    return doc.Layers.Add(item)


def attributes(doc, name, rgb):
    a = Rhino.DocObjects.ObjectAttributes()
    a.LayerIndex = layer(doc, name, rgb)
    a.ObjectColor = SD.Color.FromArgb(*rgb)
    a.ColorSource = Rhino.DocObjects.ObjectColorSource.ColorFromObject
    a.SetUserString("replay_owned", "facade-bim")
    return a


def panel_objects(doc):
    settings = Rhino.DocObjects.ObjectEnumeratorSettings()
    settings.ObjectTypeFilter = Rhino.DocObjects.ObjectType.InstanceReference
    settings.NormalObjects = settings.HiddenObjects = settings.LockedObjects = True
    settings.IdefObjects = False
    return [obj for obj in doc.Objects.GetObjectList(settings) if obj.Attributes.GetUserString("pid")]


def apply(doc, objects, day):
    objects = [doc.Objects.FindId(obj.Id) for obj in objects]
    rows = [{key: obj.Attributes.GetUserString(key) for key in KEYS} for obj in objects]
    by_pid = {row["pid"]: row for row in rows}
    state = snapshot(rows, day, capacity=len(site.slots()))  # 先校验，失败时不改文档
    dimensions = {}
    transforms = {}
    for obj in objects:
        pid = obj.Attributes.GetUserString("pid")
        try:
            dims = tuple(float(by_pid[pid][key]) for key in ("w_mm", "h_mm"))
        except (TypeError, ValueError):
            raise ValueError("板块制作尺寸属性缺失或无效：" + pid)
        if not all(math.isfinite(value) and value > 0 for value in dims):
            raise ValueError("板块制作尺寸须为有限正数：" + pid)
        dimensions[pid] = dims
        transforms[pid] = obj.InstanceXform
    owned_settings = Rhino.DocObjects.ObjectEnumeratorSettings()
    owned_settings.NormalObjects = owned_settings.HiddenObjects = owned_settings.LockedObjects = True
    owned_settings.IdefObjects = False
    for obj in list(doc.Objects.GetObjectList(owned_settings)):
        if obj.Attributes.GetUserString("replay_owned") == "facade-bim":
            if not doc.Objects.Delete(obj.Id, True):
                raise RuntimeError("无法清理已有回放对象，请解锁回放图层：" + str(obj.Id))
    for lay in doc.Layers:
        if lay.IsDeleted:
            continue
        top = lay.FullPath.split("::")[0]
        if top in ("分析", "标注", "型录", "幕墙", "结构", "施工总平面", PREFIX):
            on = top not in ("分析", "标注", "型录")
            lay.IsVisible = on
            lay.SetPersistentVisibility(on)
            lay.CommitChanges()
    for obj in list(doc.Objects.GetObjectList(owned_settings)):
        top = doc.Layers[obj.Attributes.LayerIndex].FullPath.split("::")[0]
        if (isinstance(obj.Geometry, RG.TextDot) and top == "施工总平面"
                and not obj.Attributes.GetUserString("replay_owned")):
            doc.Objects.Hide(obj.Id, True)
    for obj in objects:
        pid = obj.Attributes.GetUserString("pid")
        status = state["states"][pid]
        a = obj.Attributes.Duplicate()
        a.SetUserString("replay_date", state["date"])
        a.SetUserString("replay_state", status)
        doc.Objects.ModifyAttributes(obj.Id, a, True)
        if status in ("installed", "installing"):
            doc.Objects.Show(obj.Id, True)
        else:
            doc.Objects.Hide(obj.Id, True)
        if status == "installing":
            w, h = dimensions[pid]
            mesh = RG.Mesh()
            for x, z in ((0, 0), (w, 0), (w, h), (0, h)):
                pt = RG.Point3d(x, -35, z)
                pt.Transform(transforms[pid])
                mesh.Vertices.Add(pt)
            mesh.Faces.AddFace(0, 1, 2, 3)
            mesh.Normals.ComputeNormals()
            highlight = attributes(doc, "当日安装", (245, 145, 35))
            highlight.Name = "当日安装 " + pid
            doc.Objects.AddMesh(mesh, highlight)
    slots = site.slots()
    for s in state["active_stillages"]:
        x0, y0, x1, y1 = slots[s["slot"]]
        a = attributes(doc, "在场运输架（示意）", (45, 160, 190))
        a.Name = s["stillage"]
        for key in ("stillage", "slot", "remaining"):
            a.SetUserString(key, str(s[key]))
        a.SetUserString("geometry_role", "schematic stillage occupancy; not packing geometry")
        box = RG.Box(RG.BoundingBox(RG.Point3d(x0 + 300, y0 + 300, 30),
                                   RG.Point3d(x1 - 300, y1 - 300, 1600))).ToBrep()
        doc.Objects.AddBrep(box, a)
        label = "%s:%d" % (s["stillage"], s["remaining"])
        dot = RG.TextDot(label, RG.Point3d((x0 + x1) / 2, (y0 + y1) / 2, 1900))
        dot.FontHeight = 11
        doc.Objects.AddTextDot(dot, a)
    c = state["counts"]
    title = "%s | 已安装 %d + 当日 %d | 待装 %d | 未到场 %d | 运输架 %d/%d" % (
        state["date"], c["installed"], c["installing"], c["on_site"], c["not_delivered"],
        state["occupied_slots"], state["slot_capacity"])
    doc.Objects.AddTextDot(title, RG.Point3d(24000, -3000, 35500), attributes(doc, "日期与统计", (30, 45, 60)))
    doc.Strings.SetString("replay_date", state["date"])
    doc.Strings.SetString("replay_semantics", state["semantics"])
    visible = sum(not doc.Objects.FindId(obj.Id).IsHidden for obj in objects)
    if visible != state["visible_panels"]:
        raise RuntimeError("Rhino 显示板块数量不符：%d != %d" % (visible, state["visible_panels"]))
    state["native_visible_panels"] = visible
    doc.Views.Redraw()
    Rhino.RhinoApp.Wait()
    print(title)
    return state


def export(doc, state):
    out_dir = os.path.join(ROOT, "model", "replay")
    os.makedirs(out_dir, exist_ok=True)
    stem = "facade_" + state["date"]
    view = doc.Views.Find("Perspective", False) or doc.Views.ActiveView
    doc.Views.ActiveView = view
    view.Maximized = True
    vp = view.ActiveViewport
    vp.ChangeToPerspectiveProjection(True, 35)
    vp.SetCameraLocations(RG.Point3d(24500, 3500, 13500), RG.Point3d(-55000, -95000, 60000))
    modes = list(Rhino.Display.DisplayModeDescription.GetDisplayModes())
    mode = next((m for m in modes if m.EnglishName == "Shaded"), None)
    if mode:
        vp.DisplayMode = mode
    render_objects = list(doc.Objects)
    for definition in doc.InstanceDefinitions:
        if not definition.IsDeleted:
            render_objects.extend(definition.GetObjects())
    for obj in render_objects:
        if isinstance(obj.Geometry, (RG.Brep, RG.Extrusion)):
            obj.CreateMeshes(RG.MeshType.Render, RG.MeshingParameters.Default, False)
    doc.Views.Redraw()
    Rhino.RhinoApp.Wait()
    capture_mode = Rhino.Display.DisplayModeDescription.GetDisplayMode(vp.DisplayMode.Id)
    display = capture_mode.DisplayAttributes
    display.ShadingEnabled = True
    display.MeshSpecificAttributes.ShowMeshWires = False
    display.ShowSurfaceEdges = False
    display.ViewSpecificAttributes.DrawGrid = False
    display.ViewSpecificAttributes.DrawGridAxes = False
    display.ViewSpecificAttributes.DrawWorldAxes = False
    try:
        warmup = view.CaptureToBitmap(SD.Size(1600, 1000), display)
        if warmup is not None:
            warmup.Dispose()
        view.Redraw()
        Rhino.RhinoApp.Wait()
        bmp = view.CaptureToBitmap(SD.Size(1600, 1000), display)
    finally:
        capture_mode.Dispose()
    if bmp is None:
        raise RuntimeError("Rhino 日期截图失败")
    try:
        bmp.Save(os.path.join(out_dir, stem + ".png"), SD.Imaging.ImageFormat.Png)
    finally:
        bmp.Dispose()
    neutral = os.path.join(os.environ.get("PUBLIC", "C:\\Users\\Public"), "Documents", "facade-bim", "replay")
    os.makedirs(neutral, exist_ok=True)
    opt = Rhino.FileIO.FileWriteOptions()
    opt.IncludeRenderMeshes = False
    opt.IncludeHistory = False
    opt.UpdateDocumentPath = False
    modified = doc.Modified
    temp_path = os.path.join(neutral, stem + ".3dm")
    if not doc.WriteFile(temp_path, opt):
        raise RuntimeError("Rhino 日期模型存盘失败")
    doc.Modified = modified
    shutil.copyfile(temp_path, os.path.join(out_dir, stem + ".3dm"))
    state["rhino"] = str(Rhino.RhinoApp.Version)
    with open(os.path.join(out_dir, stem + ".json"), "w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2, sort_keys=True)


def main():
    dates = os.environ.get("FACADE_REPLAY_DATES")
    if dates:
        if not Rhino.RhinoDoc.OpenFile(os.path.join(ROOT, "model", "facade_bim.3dm")):
            raise RuntimeError("打不开已提交的 facade_bim.3dm")
    doc = Rhino.RhinoDoc.ActiveDoc
    objects = panel_objects(doc)
    if not objects:
        raise ValueError("请先打开 facade_bim.3dm 或生成的日期模型，再运行此脚本")
    if dates:
        snapshots = []
        for value in dates.split(","):
            state = apply(doc, objects, date.fromisoformat(value))
            export(doc, state)
            snapshots.append({k: state[k] for k in ("date", "counts", "occupied_slots", "native_visible_panels")})
        doc.Modified = False
        return {"ok": True, "rhino": str(Rhino.RhinoApp.Version), "snapshots": snapshots}
    current = doc.Strings.GetValue("replay_date") or min(obj.Attributes.GetUserString("install_date") for obj in objects)
    day = date.fromisoformat(current)
    state = apply(doc, objects, day)
    while True:
        value = rs.GetString("施工日期 YYYY-MM-DD，Next/Previous 为相邻日，Save 导出，Done 结束", day.isoformat(),
                             ["Next", "Previous", "Save", "Done"])
        if value is None or value.lower() == "done":
            break
        if value.lower() == "save":
            export(doc, state)
            continue
        try:
            new_day = (day + timedelta(days=1) if value.lower() == "next" else
                       day - timedelta(days=1) if value.lower() == "previous" else date.fromisoformat(value))
        except ValueError:
            print("日期格式应为 YYYY-MM-DD")
            continue
        day = new_day
        state = apply(doc, objects, day)
    return {"ok": True, "interactive": True}


if __name__ == "__main__":
    started = time.time()
    try:
        log = main()
    except Exception:
        log = {"ok": False, "error": traceback.format_exc()}
        print(log["error"])
    if os.environ.get("FACADE_REPLAY_DATES"):
        log["seconds"] = round(time.time() - started, 1)
        out = os.path.join(ROOT, "model", "replay")
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "run_log.json"), "w", encoding="utf-8") as stream:
            json.dump(log, stream, ensure_ascii=False, indent=2)
        Rhino.RhinoDoc.ActiveDoc.Modified = False

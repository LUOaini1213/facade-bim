#! python3
"""原生 Rhino 三维质量与真实 Eto 时间轴事件检查；不存回源模型。"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from datetime import date

ROOT = os.environ.get("FACADE_BIM_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "rhino"))
import Rhino
import Rhino.Geometry as RG
import spatial_quality as spatial


def gather(doc, day=None):
    from replay_install import panel_objects
    units = []
    cache = {}
    for obj in panel_objects(doc):
        if day and date.fromisoformat(obj.Attributes.GetUserString("install_date")) > day:
            continue
        definition = obj.InstanceDefinition
        if definition.Id not in cache:
            cache[definition.Id] = [(item.Attributes.Name, item.Geometry.DuplicateBrep()) for item in definition.GetObjects()]
        parts = []
        for index, (name, geometry) in enumerate(cache[definition.Id]):
            geo = geometry.DuplicateBrep()
            geo.Transform(obj.InstanceXform)
            part_name = "%s#%d" % (name, index + 1)
            try:
                parts.append(spatial.part(geo, part_name))
            except Exception as error:
                raise ValueError("%s [%s] / %s: %s" % (obj.Attributes.GetUserString("pid"), obj.Id, part_name, error)) from error
        units.append(spatial.unit(obj.Attributes.GetUserString("pid"), parts, str(obj.Id), "panel"))
    settings = Rhino.DocObjects.ObjectEnumeratorSettings()
    settings.NormalObjects = settings.HiddenObjects = settings.LockedObjects = True
    settings.IdefObjects = False
    for obj in doc.Objects.GetObjectList(settings):
        path = doc.Layers[obj.Attributes.LayerIndex].FullPath
        if (path.startswith("结构::") or path == "幕墙::转角立柱") and isinstance(obj.Geometry, (RG.Brep, RG.Extrusion)):
            brep = obj.Geometry.ToBrep() if isinstance(obj.Geometry, RG.Extrusion) else obj.Geometry.DuplicateBrep()
            role = "corner_post" if path == "幕墙::转角立柱" else "structure"
            try:
                parts = [spatial.part(brep, path)]
            except Exception as error:
                raise ValueError("%s [%s] / %s: %s" % (obj.Attributes.Name, obj.Id, path, error)) from error
            units.append(spatial.unit(obj.Attributes.Name or str(obj.Id), parts, str(obj.Id), role))
    return units


def model_fixture(units):
    panel = next(item for item in units if item["id"] == "S-L1-01")
    post = next(item for item in units if item["role"] == "corner_post" and
                abs(item["bbox"].Min.X) < 0.01 and abs(item["bbox"].Min.Y) < 0.01)
    copied = []
    for item in panel["parts"]:
        geometry = item["brep"].DuplicateBrep()
        geometry.Transform(RG.Transform.Translation(-200, 0, 0))
        copied.append(spatial.part(geometry, item["name"]))
    shifted = spatial.unit(panel["id"] + " (fixture shifted -200mm)", copied, panel["guid"])
    hits, _, unresolved = spatial.check_pair(shifted, post, 10, 0.01)
    assert hits and not unresolved, "实际板块移入西南转角柱必须检出三维实体碰撞"
    return {"ok": True, "source_panel": panel["id"], "corner_post_guid": post["guid"],
            "copied_panel_shift_mm": [-200, 0, 0], "volume_events": len(hits), "source_document_unchanged": True}


def main():
    import probe_spatial
    probe = probe_spatial.main()
    if not probe["ok"]:
        raise RuntimeError("原生空间 API 探针失败；见 model/quality/spatial_probe.json")
    model = os.path.join(ROOT, "model", "facade_bim.3dm")
    digest = hashlib.sha256(open(model, "rb").read()).hexdigest()
    if not Rhino.RhinoDoc.OpenFile(model):
        raise RuntimeError("打不开幕墙源模型")
    doc = Rhino.RhinoDoc.ActiveDoc
    if doc.ModelUnitSystem != Rhino.UnitSystem.Millimeters:
        raise ValueError("该质量检查要求模型单位为毫米")
    clearance = float(os.environ.get("FACADE_CLEARANCE_MM", "10"))
    value = os.environ.get("FACADE_QUALITY_DATE")
    day = date.fromisoformat(value) if value else None
    out = {"rhino": str(Rhino.RhinoApp.Version), "source_sha256": digest,
           "date": value or "all final geometry", "fixtures": spatial.run_fixtures()}
    if os.environ.get("FACADE_QUALITY_MODE") != "timeline":
        units = gather(doc, day)
        if not day or any(item["id"] == "S-L1-01" for item in units):
            out["model_fixture"] = model_fixture(units)
        out["spatial"] = spatial.analyse(units, clearance, max(doc.ModelAbsoluteTolerance, 0.01))
    if os.environ.get("FACADE_QUALITY_MODE") != "spatial":
        import timeline
        # The real export/reentry test saves the sentinel too. Keep all QA
        # snapshots and Rhino's intermediate native save outside the delivery.
        with tempfile.TemporaryDirectory(prefix="facade_timeline_qa_") as folder:
            os.makedirs(os.path.join(folder, "model"))
            copied_model = os.path.join(folder, "model", "facade_bim.3dm")
            shutil.copyfile(model, copied_model)
            old_roots = timeline.ROOT, timeline.replay.ROOT
            old_public = os.environ.get("PUBLIC")
            try:
                timeline.ROOT = timeline.replay.ROOT = folder
                os.environ["PUBLIC"] = folder
                sentinel = doc.Objects.AddTextDot("unrelated user annotation", RG.Point3d(0, 0, 0))
                try:
                    out["timeline"] = timeline.run_qa(doc)
                    assert not doc.Objects.FindId(sentinel).IsHidden, "回放不得隐藏无关用户标注"
                finally:
                    doc.Objects.Delete(sentinel, True)
                for extension in ("3dm", "png", "json"):
                    exported = os.path.join(folder, "model", "replay", "facade_2026-11-23." + extension)
                    assert os.path.isfile(exported) and os.path.getsize(exported) > 0, "真实 QA 导出产物缺失"
                out["timeline"]["qa_export_isolated"] = True
                out["timeline"]["document_lifecycle_guarded"] = timeline.run_lifecycle_qa(doc)
                with open(copied_model, "rb") as stream:
                    assert hashlib.sha256(stream.read()).hexdigest() == digest
                doc = Rhino.RhinoDoc.ActiveDoc
            finally:
                timeline.ROOT, timeline.replay.ROOT = old_roots
                if old_public is None:
                    os.environ.pop("PUBLIC", None)
                else:
                    os.environ["PUBLIC"] = old_public
                active = Rhino.RhinoDoc.ActiveDoc
                if active:
                    active.Modified = False
                # Release the lifecycle test's temporary document before the
                # surrounding directory is removed, including failure paths.
                assert Rhino.RhinoDoc.OpenFile(model), "QA 结束后无法重新打开源模型"
                doc = Rhino.RhinoDoc.ActiveDoc
                assert os.path.normcase(os.path.abspath(doc.Path)) == os.path.normcase(os.path.abspath(model))
    assert hashlib.sha256(open(model, "rb").read()).hexdigest() == digest
    out["source_preserved"] = True
    out["ok"] = all(out[key]["ok"] for key in ("fixtures", "spatial", "timeline") if key in out)
    doc.Modified = False
    return out


if __name__ == "__main__":
    start = time.monotonic()
    try:
        result = main()
    except Exception:
        result = {"ok": False, "error": traceback.format_exc()}
    result["seconds"] = round(time.monotonic() - start, 2)
    out = os.path.join(ROOT, "model", "quality")
    os.makedirs(out, exist_ok=True)
    mode = os.environ.get("FACADE_QUALITY_MODE", "all")
    with open(os.path.join(out, "native_" + mode + ".json"), "w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    if Rhino.RhinoDoc.ActiveDoc:
        Rhino.RhinoDoc.ActiveDoc.Modified = False

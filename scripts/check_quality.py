"""独立读取原生质量报告，核对源模型身份、完整范围及告警定位，并生成 HTML。"""
import argparse
import hashlib
import html
import json
import math
from pathlib import Path
import sys
import rhino3dm

ROOT = Path(__file__).resolve().parents[1]


def verify(path):
    source = ROOT / "model" / "facade_bim.3dm"
    data = json.loads(path.read_text(encoding="utf-8"))
    mode = {"native_all": "all", "native_spatial": "spatial", "native_timeline": "timeline"}.get(path.stem, "all")
    required = {"fixtures", "spatial", "timeline"} if mode == "all" else {"fixtures", mode}
    assert required <= data.keys(), "missing required native quality stages: " + ", ".join(sorted(required - data.keys()))
    assert data.get("source_preserved"), data.get("error", "native run did not finish")
    assert data["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest(), "stale native report"
    assert data["fixtures"]["ok"] and len(data["fixtures"]["checks"]) == 6
    model = rhino3dm.File3dm.Read(str(source))
    assert model.Settings.ModelUnitSystem == rhino3dm.UnitSystem.Millimeters, "source model units changed"
    ids, parts, part_names = {}, {}, {}
    definitions = {d.Id: d for d in model.InstanceDefinitions}
    objects = {o.Attributes.Id: o for o in model.Objects}
    for obj in model.Objects:
        pid = obj.Attributes.GetUserString("pid")
        if pid and isinstance(obj.Geometry, rhino3dm.InstanceReference):
            if data["date"] != "all final geometry" and obj.Attributes.GetUserString("install_date") > data["date"]:
                continue
            ids[pid] = str(obj.Attributes.Id)
            members = definitions[obj.Geometry.ParentIdefId].GetObjectIds()
            parts[pid] = len(members)
            part_names[pid] = {"%s#%d" % (objects[ident].Attributes.Name, index + 1)
                               for index, ident in enumerate(members)}
        elif isinstance(obj.Geometry, (rhino3dm.Brep, rhino3dm.Extrusion)) and not obj.Attributes.IsInstanceDefinitionObject:
            layer = model.Layers.FindIndex(obj.Attributes.LayerIndex)
            if layer.FullPath.startswith("结构::") or layer.FullPath == "幕墙::转角立柱":
                key = obj.Attributes.Name or str(obj.Attributes.Id)
                ids[key] = str(obj.Attributes.Id)
                parts[key] = 1
                part_names[key] = {layer.FullPath}
    if "spatial" in data:
        q = data["spatial"]
        if data["date"] == "all final geometry":
            assert q["units"] == 823 and q["solid_parts"] == 7477
            assert data["model_fixture"]["ok"] and data["model_fixture"]["volume_events"] > 0
        assert q["units"] == len(ids), "incomplete spatial scope"
        assert q["solid_parts"] == sum(parts.values()), "missing actual Rhino parts"
        assert q["ok"] == (not q["clashes"] and not q["unresolved"])
        assert q["clearance_ok"] == (not q["clashes"] and not q["unresolved"] and not q["clearance_events"])
        for event in q["clashes"] + q["clearance_events"] + q["unresolved"]:
            assert event["a"] != event["b"]
            for side in ("a", "b"):
                assert ids[event[side]] == event[side + "_guid"], "event identity not in source"
                assert event[side + "_part"] in part_names[event[side]], "event part not in source"
        for event in q["clashes"] + q["clearance_events"]:
            assert len(event["point_mm"]) == 3 and all(math.isfinite(v) for v in event["point_mm"])
            if "intersection_volume_mm3" in event:
                assert event["intersection_volume_mm3"] > 0
        rows = []
        for category, events in (("VOLUME CLASH", q["clashes"]), ("CLEARANCE / CONTACT", q["clearance_events"]),
                                 ("UNRESOLVED", q["unresolved"])):
            for event in events:
                rows.append("<tr><td>%s</td><td>%s / %s</td><td>%s / %s</td><td>%s</td><td>%s</td></tr>" % (
                    category, html.escape(event["a"]), html.escape(event["a_part"]), html.escape(event["b"]),
                    html.escape(event["b_part"]), html.escape(str(event.get("point_mm", "unavailable"))),
                    html.escape(event.get("reason", event.get("kind", "volume overlap")))))
        document = ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>Rhino 3D quality</title>"
                    "<style>body{font:15px system-ui;margin:30px;color:#203045}table{border-collapse:collapse}"
                    "td,th{border:1px solid #cbd5df;padding:7px;text-align:left}</style>"
                    "<h1>Rhino 三维碰撞与净距报告</h1><p>%d 构件，%d 实体零件；体积碰撞 %d；净距/接触事件 %d；未决 %d。</p>"
                    "<p>净距阈值 %.2f mm。接触单独列出，需要按连接意图复核；网格见证点不表示精确最短距离。</p>"
                    "<table><tr><th>类别</th><th>构件 A / 零件</th><th>构件 B / 零件</th><th>位置 mm</th><th>状态</th></tr>%s</table></html>") % (
                        q["units"], q["solid_parts"], len(q["clashes"]), len(q["clearance_events"]), len(q["unresolved"]),
                        q["clearance_mm"], "".join(rows))
        path.with_suffix(".html").write_text(document, encoding="utf-8")
    if "timeline" in data:
        q = data["timeline"]
        assert q["date_events"] >= 5
        for key in ("ok", "real_eto_timer", "reverse_seek", "pause", "invalid_input_preserves_date", "latest_seek_wins",
                    "busy_save_guarded", "export_reentry_guarded", "document_lifecycle_guarded", "end_stops_playback", "close_stops_timer"):
            assert q[key], "timeline runtime failure: " + key
    assert data["ok"] == all(data[key]["ok"] for key in ("fixtures", "spatial", "timeline") if key in data), "inconsistent aggregate quality status"
    return data


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    paths = args.paths or sorted((ROOT / "model" / "quality").glob("native_*.json"))
    if not paths:
        parser.error("缺原生报告；先运行 scripts/run_quality.py")
    passed = True
    for path in paths:
        path = path.resolve()
        data = verify(path)
        passed = passed and data["ok"]
        label = path.relative_to(ROOT) if ROOT in path.parents else path
        print(("PASS " if data["ok"] else "FAIL ") + str(label))
    if not passed:
        sys.exit(1)


if __name__ == "__main__":
    main()

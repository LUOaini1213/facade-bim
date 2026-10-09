"""无需 Rhino，独立读回日期 .3dm，核对真实显隐、排程、编号、块变换及在场架位。"""
import argparse
from collections import Counter
import json
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import rhino3dm
from facade.replay import snapshot
from facade import site


def panels(model):
    out = {}
    for obj in model.Objects:
        if isinstance(obj.Geometry, rhino3dm.InstanceReference) and obj.Attributes.GetUserString("pid"):
            pid = obj.Attributes.GetUserString("pid")
            if pid in out:
                raise AssertionError("duplicate panel: " + pid)
            out[pid] = obj
    return out


def verify(path, source):
    model = rhino3dm.File3dm.Read(str(path))
    assert model is not None, "unreadable replay .3dm"
    assert source is not None and source.Settings.ModelUnitSystem == rhino3dm.UnitSystem.Millimeters, "source must use millimetres"
    assert model.Settings.ModelUnitSystem == source.Settings.ModelUnitSystem, "replay model units changed"
    info = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    original, actual = panels(source), panels(model)
    assert set(actual) == set(original), "panel identity set changed"
    rows = [dict(obj.Attributes.GetUserStrings()) for obj in original.values()]
    expected = snapshot(rows, info["date"], len(site.slots()))
    for key in ("counts", "states", "active_stillages", "occupied_slots", "visible_panels", "total"):
        assert info[key] == expected[key], "JSON disagrees with source model: " + key
    visible = 0
    for pid, obj in actual.items():
        base = original[pid]
        assert obj.Attributes.Id == base.Attributes.Id, "object id changed: " + pid
        assert obj.Geometry.ParentIdefId == base.Geometry.ParentIdefId, "block changed: " + pid
        for i in range(4):
            for j in range(4):
                key = "M%d%d" % (i, j)
                assert getattr(obj.Geometry.Xform, key) == getattr(base.Geometry.Xform, key), "placement changed: " + pid
        for key, value in base.Attributes.GetUserStrings():
            assert obj.Attributes.GetUserString(key) == value, "BIM attribute changed: " + pid + "/" + key
        assert obj.Attributes.GetUserString("replay_date") == info["date"], "missing native date"
        assert obj.Attributes.GetUserString("replay_state") == expected["states"][pid], "wrong native state"
        shown = obj.Attributes.Mode != rhino3dm.ObjectMode.Hidden
        assert shown == (expected["states"][pid] in ("installing", "installed")), "wrong saved visibility: " + pid
        if shown:
            visible += 1
            assert model.Layers.FindIndex(obj.Attributes.LayerIndex).Visible, "visible panel on hidden layer"
    assert visible == info["native_visible_panels"] == expected["visible_panels"]
    racks, highlights = {}, []
    for obj in model.Objects:
        attrs = obj.Attributes
        if attrs.GetUserString("replay_owned") != "facade-bim":
            continue
        if attrs.GetUserString("geometry_role") and isinstance(obj.Geometry, rhino3dm.Brep):
            sid = attrs.GetUserString("stillage")
            assert sid not in racks, "duplicate native rack: " + sid
            racks[sid] = {"stillage": sid, "slot": int(attrs.GetUserString("slot")),
                          "remaining": int(attrs.GetUserString("remaining"))}
        if isinstance(obj.Geometry, rhino3dm.Mesh):
            highlights.append(attrs.Name.removeprefix("当日安装 "))
    assert sorted(racks.values(), key=lambda item: item["stillage"]) == expected["active_stillages"]
    assert Counter(highlights) == Counter(pid for pid, state in expected["states"].items() if state == "installing")
    data = path.with_suffix(".png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", data[16:24]) == (1600, 1000)
    print("PASS %s: %d panels visible, %d racks; source BIM identities/attributes/transforms preserved" % (
        path.name, visible, len(racks)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    paths = args.paths or sorted((ROOT / "model" / "replay").glob("facade_*.3dm"))
    if not args.paths:
        required = {"facade_" + day for day in ("2026-10-30", "2026-11-02", "2026-11-23", "2026-12-20")}
        assert required <= {path.stem for path in paths}, "missing committed verification date snapshots"
    if not paths:
        parser.error("没有回放文件；先运行 scripts/run_replay.py")
    source = rhino3dm.File3dm.Read(str(ROOT / "model" / "facade_bim.3dm"))
    for path in paths:
        verify(path, source)


if __name__ == "__main__":
    main()

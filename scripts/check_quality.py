"""独立读取原生质量报告，核对源模型身份、完整范围及告警定位，并生成 HTML。"""
import argparse
from collections import Counter
import hashlib
import html
import json
import math
from pathlib import Path
import sys
import rhino3dm

ROOT = Path(__file__).resolve().parents[1]
EPSILON_MM = 1e-7
KINDS = ("contact", "below_clearance", "at_clearance", "threshold_candidate")


def _point(point, transform=None):
    values = (point.X, point.Y, point.Z)
    if transform is not None:
        assert all(abs(getattr(transform, "M3%d" % i) - float(i == 3)) <= EPSILON_MM for i in range(4)), "non-affine source transform"
        values = tuple(sum(getattr(transform, "M%d%d" % (row, col)) * values[col] for col in range(3))
                       + getattr(transform, "M%d3" % row) for row in range(3))
    assert all(math.isfinite(value) for value in values), "non-finite source geometry"
    return values


def source_box_bounds(geometry, transform=None):
    """Independently prove actual Rhino faces/edges/corners, never assume an AABB is a solid."""
    if isinstance(geometry, rhino3dm.Extrusion):
        geometry = geometry.ToBrep()
    if (not isinstance(geometry, rhino3dm.Brep) or not geometry.IsValid or not geometry.IsSolid
            or len(geometry.Vertices) != 8 or len(geometry.Edges) != 12 or len(geometry.Faces) != 6):
        return None
    points = [_point(vertex.Location, transform) for vertex in geometry.Vertices]
    bounds = [[min(point[i] for point in points) for i in range(3)],
              [max(point[i] for point in points) for i in range(3)]]
    if not all(bounds[1][i] > bounds[0][i] for i in range(3)):
        return None
    codes = []
    for point in points:
        code = []
        for i in range(3):
            if abs(point[i] - bounds[0][i]) <= EPSILON_MM:
                code.append(0)
            elif abs(point[i] - bounds[1][i]) <= EPSILON_MM:
                code.append(1)
            else:
                return None
        codes.append(tuple(code))
    if len(set(codes)) != 8:
        return None
    for edge in geometry.Edges:
        a, b = _point(edge.PointAtStart, transform), _point(edge.PointAtEnd, transform)
        if not edge.IsLinear(EPSILON_MM) or sum(abs(b[i] - a[i]) > EPSILON_MM for i in range(3)) != 1:
            return None
    supports = set()
    for face in geometry.Faces:
        if not face.IsPlanar(EPSILON_MM) or len(face.Loops) != 1:
            return None
        patch = face.DuplicateFace(False)
        if len(patch.Edges) != 4 or len(patch.Vertices) != 4:
            return None
        corners = [_point(vertex.Location, transform) for vertex in patch.Vertices]
        support = [(axis, side) for axis in range(3) for side in (0, 1)
                   if all(abs(point[axis] - bounds[side][axis]) <= EPSILON_MM for point in corners)]
        if len(support) != 1:
            return None
        supports.add(support[0])
    return bounds if len(supports) == 6 else None


def _finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def box_gap(a, b):
    return math.sqrt(sum(max(0.0, a[0][i] - b[1][i], b[0][i] - a[1][i]) ** 2 for i in range(3)))


def source_boxes(source_parts):
    """Cache the shared local part proof, then independently certify transformed corners."""
    local, result = {}, {}
    for key, (geometry, transform) in source_parts.items():
        ident = id(geometry)
        if ident not in local:
            local[ident] = source_box_bounds(geometry)
        bounds = local[ident]
        if bounds is None or transform is None:
            result[key] = bounds
            continue
        points = [_point(rhino3dm.Point3d(x, y, z), transform)
                  for x in (bounds[0][0], bounds[1][0]) for y in (bounds[0][1], bounds[1][1])
                  for z in (bounds[0][2], bounds[1][2])]
        world = [[min(point[i] for point in points) for i in range(3)],
                 [max(point[i] for point in points) for i in range(3)]]
        codes = []
        for point in points:
            code = []
            for i in range(3):
                if abs(point[i] - world[0][i]) <= EPSILON_MM:
                    code.append(0)
                elif abs(point[i] - world[1][i]) <= EPSILON_MM:
                    code.append(1)
                else:
                    break
            codes.append(tuple(code))
        result[key] = world if len(set(codes)) == 8 and all(len(code) == 3 for code in codes) else None
    return result


def clearance_key(a, ap, b, bp):
    return tuple(sorted(((a, ap), (b, bp))))


def verify_box_event_coverage(proven, events, threshold, unresolved=()):
    """Re-enumerate all certified source-box pairs; deleting a contact must not wash the report green."""
    units = {}
    for (ident, name), bounds in proven.items():
        if bounds is not None:
            units.setdefault(ident, []).append((name, bounds))
    ordered = []
    for ident, parts in units.items():
        box = [[min(bounds[0][i] for _, bounds in parts) for i in range(3)],
               [max(bounds[1][i] for _, bounds in parts) for i in range(3)]]
        ordered.append((ident, parts, box))
    ordered.sort(key=lambda item: (item[2][0][0], item[0]))
    active, expected = [], set()
    for b, bp, bbox in ordered:
        active = [a for a in active if a[2][1][0] + threshold + EPSILON_MM >= bbox[0][0]]
        for a, ap, abox in active:
            if box_gap(abox, bbox) > threshold + EPSILON_MM:
                continue
            for an, av in ap:
                for bn, bv in bp:
                    # Volume overlap belongs to the independent native clash report.
                    if all(min(av[1][i], bv[1][i]) > max(av[0][i], bv[0][i]) for i in range(3)):
                        continue
                    if box_gap(av, bv) <= threshold + EPSILON_MM:
                        expected.add(clearance_key(a, an, b, bn))
        active.append((b, bp, bbox))
    actual = [clearance_key(event["a"], event["a_part"], event["b"], event["b_part"]) for event in events
              if proven[(event["a"], event["a_part"])] is not None and proven[(event["b"], event["b_part"])] is not None]
    assert len(actual) == len(set(actual)), "duplicate source-box clearance event"
    failed = []
    for event in unresolved:
        a = proven[(event["a"], event["a_part"])]
        b = proven[(event["b"], event["b_part"])]
        if a is not None and b is not None:
            assert box_gap(a, b) <= threshold + EPSILON_MM, "unresolved pair outside source candidate range"
            failed.append(clearance_key(event["a"], event["a_part"], event["b"], event["b_part"]))
    assert len(failed) == len(set(failed)), "duplicate unresolved source-box pair"
    assert not set(actual).intersection(failed), "pair reported as both resolved and unresolved"
    assert set(actual) <= expected and expected <= set(actual).union(failed), "missing or extra independently enumerated source-box clearance events"


def verify_clearance_event(event, bounds, threshold, tolerance):
    """Recompute geometry and kind from source, so a changed label/gap cannot turn a failure green."""
    kind = event["kind"]
    assert kind in KINDS, "unknown clearance classification"
    assert _finite(event["required_clearance_mm"]) and event["required_clearance_mm"] == threshold, "event threshold changed"
    witness = event["mesh_witness_available"]
    assert type(witness) is bool, "invalid native witness flag"
    contact = event["mesh_contact_search"]
    assert _finite(contact["tolerance_mm"]) and contact["tolerance_mm"] == tolerance and type(contact["hit"]) is bool
    if witness:
        assert _finite(event["witness_radius_mm"]) and event["witness_radius_mm"] >= 0
    else:
        assert "witness_radius_mm" not in event, "analytic point must not pretend to be a mesh witness"
    if not all(bound is not None for bound in bounds):
        assert kind == "threshold_candidate" and event.get("review_required") is True, "non-box distance must remain unverified"
        assert "actual_gap_mm" not in event and "distance_evidence" not in event, "unproved geometry claims an exact distance"
        if not witness:
            assert event["point_source"] == "unverified_bbox_candidate_midpoint"
        return kind
    actual = box_gap(*bounds)
    proof = event["distance_evidence"]
    assert proof["method"] == "proved_axis_aligned_box_gap" and proof["axis_box_proof"] == [True, True]
    assert proof["comparison_epsilon_mm"] == EPSILON_MM, "comparison epsilon changed"
    assert _finite(event["actual_gap_mm"]) and event["actual_gap_mm"] >= 0
    assert math.isclose(event["actual_gap_mm"], actual, rel_tol=0.0, abs_tol=EPSILON_MM), "claimed gap differs from actual Rhino geometry"
    for side, expected in zip(("a", "b"), bounds):
        claimed = proof[side + "_bounds_mm"]
        assert len(claimed) == 2 and all(len(point) == 3 for point in claimed)
        assert all(_finite(claimed[i][j]) and abs(claimed[i][j] - expected[i][j]) <= EPSILON_MM
                   for i in range(2) for j in range(3)), "proof bounds differ from actual Rhino geometry"
    expected_kind = ("contact" if actual <= EPSILON_MM else "at_clearance"
                     if math.isclose(actual, threshold, rel_tol=0.0, abs_tol=EPSILON_MM)
                     else "below_clearance" if actual < threshold else None)
    assert kind == expected_kind, "clearance kind differs from independently proved distance"
    if not witness:
        expected_point = [(max(bounds[0][0][i], bounds[1][0][i]) + min(bounds[0][1][i], bounds[1][1][i])) / 2
                          for i in range(3)]
        assert all(abs(a - b) <= EPSILON_MM for a, b in zip(event["point_mm"], expected_point))
    return kind


def verify_boundary_checks(fixtures):
    boundary = fixtures["boundary_checks"]
    assert boundary["ok"] is True and boundary["model_gap_mm"] == 5.0 and boundary["thresholds_mm"] == [6, 5, 4]
    rows = boundary["diagnostics"]
    assert len(rows) == 3
    bounds = [[[0.0, 0.0, 0.0], [10.0, 10.0, 10.0]], [[0.0, 0.0, 15.0], [10.0, 10.0, 25.0]]]
    for row, threshold, kind in zip(rows, (6, 5, 4), ("below_clearance", "at_clearance", None)):
        assert row["clearance_mm"] == threshold and row["expected_kind"] == kind
        assert not row["clashes"] and not row["unresolved"]
        assert row["aggregate_clearance_ok"] is (threshold <= 5), "boundary fixture aggregate changed"
        assert len(row["clearance_events"]) == int(kind is not None)
        for event in row["clearance_events"]:
            assert verify_clearance_event(event, bounds, threshold, 0.01) == kind
    narrow = boundary["subthreshold_check"]
    assert narrow["clearance_mm"] == 10 and not narrow["clashes"] and not narrow["unresolved"]
    assert narrow["aggregate_clearance_ok"] is False and len(narrow["clearance_events"]) == 1
    narrow_bounds = [bounds[0], [[0.0, 0.0, 19.999], [10.0, 10.0, 29.999]]]
    assert verify_clearance_event(narrow["clearance_events"][0], narrow_bounds, 10, 0.01) == "below_clearance"
    candidate = boundary["nonbox_candidate_check"]
    assert candidate["clearance_mm"] == 3 and not candidate["clashes"] and not candidate["unresolved"]
    assert candidate["aggregate_clearance_ok"] is False and candidate["clearance_events"]
    for event in candidate["clearance_events"]:
        assert verify_clearance_event(event, [None, None], 3, 0.01) == "threshold_candidate"
    curved = boundary["uncertified_nohit_check"]
    assert curved["clearance_mm"] == 1 and not curved["clashes"] and not curved["unresolved"]
    assert curved["aggregate_clearance_ok"] is False and len(curved["clearance_events"]) == 1
    assert verify_clearance_event(curved["clearance_events"][0], [None, None], 1, 0.01) == "threshold_candidate"
    assert curved["clearance_events"][0]["mesh_witness_available"] is False


def verify(path, write_reports=True):
    source = ROOT / "model" / "facade_bim.3dm"
    data = json.loads(path.read_text(encoding="utf-8"))
    mode = {"native_all": "all", "native_spatial": "spatial", "native_timeline": "timeline"}.get(path.stem, "all")
    required = {"fixtures", "spatial", "timeline"} if mode == "all" else {"fixtures", mode}
    assert required <= data.keys(), "missing required native quality stages: " + ", ".join(sorted(required - data.keys()))
    assert data.get("source_preserved"), data.get("error", "native run did not finish")
    assert data["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest(), "stale native report"
    assert data["fixtures"]["ok"] and len(data["fixtures"]["checks"]) == 6
    verify_boundary_checks(data["fixtures"])
    model = rhino3dm.File3dm.Read(str(source))
    assert model.Settings.ModelUnitSystem == rhino3dm.UnitSystem.Millimeters, "source model units changed"
    ids, parts, part_names, source_parts = {}, {}, {}, {}
    definitions = {d.Id: d for d in model.InstanceDefinitions}
    objects = {o.Attributes.Id: o for o in model.Objects}
    geometries = {ident: obj.Geometry for ident, obj in objects.items()}
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
            for index, ident in enumerate(members):
                name = "%s#%d" % (objects[ident].Attributes.Name, index + 1)
                source_parts[(pid, name)] = (geometries[ident], obj.Geometry.Xform)
        elif isinstance(obj.Geometry, (rhino3dm.Brep, rhino3dm.Extrusion)) and not obj.Attributes.IsInstanceDefinitionObject:
            layer = model.Layers.FindIndex(obj.Attributes.LayerIndex)
            if layer.FullPath.startswith("结构::") or layer.FullPath == "幕墙::转角立柱":
                key = obj.Attributes.Name or str(obj.Attributes.Id)
                ids[key] = str(obj.Attributes.Id)
                parts[key] = 1
                part_names[key] = {layer.FullPath}
                source_parts[(key, layer.FullPath)] = (geometries[obj.Attributes.Id], None)
    if "spatial" in data:
        q = data["spatial"]
        if data["date"] == "all final geometry":
            assert q["units"] == 823 and q["solid_parts"] == 7477
            assert data["model_fixture"]["ok"] and data["model_fixture"]["volume_events"] > 0
        assert q["units"] == len(ids), "incomplete spatial scope"
        assert q["solid_parts"] == sum(parts.values()), "missing actual Rhino parts"
        assert q["ok"] == (not q["clashes"] and not q["unresolved"])
        assert _finite(q["clearance_mm"]) and q["clearance_mm"] >= 0
        assert _finite(q["boolean_tolerance_mm"]) and q["boolean_tolerance_mm"] > 0
        assert q["clearance_comparison_epsilon_mm"] == EPSILON_MM
        counts = Counter(event["kind"] for event in q["clearance_events"])
        assert q["clearance_counts"] == {kind: counts[kind] for kind in KINDS}, "clearance counts changed"
        blocking = sum(event["kind"] != "at_clearance" for event in q["clearance_events"])
        assert q["blocking_clearance_events"] == blocking
        assert q["clearance_ok"] == (not q["clashes"] and not q["unresolved"] and not blocking)
        for event in q["clashes"] + q["clearance_events"] + q["unresolved"]:
            assert event["a"] != event["b"]
            for side in ("a", "b"):
                assert ids[event[side]] == event[side + "_guid"], "event identity not in source"
                assert event[side + "_part"] in part_names[event[side]], "event part not in source"
        for event in q["clashes"] + q["clearance_events"]:
            assert len(event["point_mm"]) == 3 and all(math.isfinite(v) for v in event["point_mm"])
            if "intersection_volume_mm3" in event:
                assert event["intersection_volume_mm3"] > 0
        proven = source_boxes(source_parts)
        for event in q["clearance_events"]:
            bounds = []
            for side in ("a", "b"):
                key = (event[side], event[side + "_part"])
                bounds.append(proven[key])
            verify_clearance_event(event, bounds, q["clearance_mm"], q["boolean_tolerance_mm"])
        verify_box_event_coverage(proven, q["clearance_events"], q["clearance_mm"], q["unresolved"])
        rows = []
        labels = {"contact": "CONTACT · 未批准接触", "below_clearance": "BELOW_CLEARANCE · 净距不足",
                  "at_clearance": "AT_CLEARANCE · 阈值边界", "threshold_candidate": "THRESHOLD_CANDIDATE · 距离待核"}
        for category, events in (("VOLUME CLASH", q["clashes"]), ("CLEARANCE / CONTACT", q["clearance_events"]),
                                 ("UNRESOLVED", q["unresolved"])):
            for event in events:
                rows.append("<tr><td>%s</td><td>%s / %s</td><td>%s / %s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                    (labels[event["kind"]] if category == "CLEARANCE / CONTACT" else category), html.escape(event["a"]), html.escape(event["a_part"]), html.escape(event["b"]),
                    html.escape(event["b_part"]), ("%.9g" % event["actual_gap_mm"] if "actual_gap_mm" in event else "待核 / 不适用"),
                    ("%.9g" % event["required_clearance_mm"] if "required_clearance_mm" in event else "—"),
                    html.escape(str(event.get("point_mm", "unavailable"))),
                    html.escape(event.get("reason", ("模型净距 %.9g mm；%s" % (event["actual_gap_mm"], event["kind"]))
                                          if "actual_gap_mm" in event else event.get("kind", "volume overlap")))))
        document = ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>Rhino 3D quality</title>"
                    "<style>body{font:15px system-ui;margin:30px;color:#203045}table{border-collapse:collapse}"
                    "td,th{border:1px solid #cbd5df;padding:7px;text-align:left}</style>"
                    "<h1>Rhino 三维碰撞与净距报告</h1><p>%d 构件，%d 实体零件；体积碰撞 %d；净距/接触事件 %d；未决 %d。</p>"
                    "<p>净距阈值 %.2f mm。真实不足 %d；阈值边界 %d；未批准接触 %d；距离待核 %d。</p>"
                    "<p>只有两个实体均经轴向盒证明才报告模型精确净距；这不是现场实测。边界记录不算不足，接触仍需按连接意图复核；网格见证点/半径不是最短间隙。</p>"
                    "<table><tr><th>类别</th><th>构件 A / 零件</th><th>构件 B / 零件</th><th>模型净距 mm</th><th>要求阈值 mm</th><th>位置 mm</th><th>状态</th></tr>%s</table></html>") % (
                        q["units"], q["solid_parts"], len(q["clashes"]), len(q["clearance_events"]), len(q["unresolved"]),
                        q["clearance_mm"], counts["below_clearance"], counts["at_clearance"], counts["contact"], counts["threshold_candidate"], "".join(rows))
        if write_reports:
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
    parser.add_argument("--no-write-reports", action="store_true", help="独立核对，不重写同名HTML")
    args = parser.parse_args()
    paths = args.paths or sorted((ROOT / "model" / "quality").glob("native_*.json"))
    if not paths:
        parser.error("缺原生报告；先运行 scripts/run_quality.py")
    passed = True
    for path in paths:
        path = path.resolve()
        data = verify(path, write_reports=not args.no_write_reports)
        passed = passed and data["ok"]
        label = path.relative_to(ROOT) if ROOT in path.parents else path
        print(("PASS " if data["ok"] else "FAIL ") + str(label))
    if not passed:
        sys.exit(1)


if __name__ == "__main__":
    main()

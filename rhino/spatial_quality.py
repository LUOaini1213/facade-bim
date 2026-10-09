"""RhinoCommon 3D 检查：闭合 Brep 真实布尔体积 + 三角网格净距筛查。

同一构件内的零件不互查；包络仅作候选筛选，不能证明实际几何相交。
网格净距是阈值检测，输出见证位置，不将该位置冒充精确最短距离。
"""
import math
import Rhino
import Rhino.Geometry as RG
from Rhino.Geometry.Intersect import MeshClash

CLEARANCE_EPSILON_MM = 1e-7  # floating-point comparison only, not a design/tolerance allowance
PLANAR_MESH_ERROR_MM = 1e-6  # certified polygonal boundary budget; enlarge a no-hit query conservatively


class InconclusiveGeometry(RuntimeError):
    pass


def xyz(point):
    return [round(point.X, 6), round(point.Y, 6), round(point.Z, 6)]


def gap(a, b):
    return math.sqrt(sum(max(0.0, getattr(a.Min, axis) - getattr(b.Max, axis),
                                 getattr(b.Min, axis) - getattr(a.Max, axis)) ** 2 for axis in "XYZ"))


def overlap(a, b, tolerance):
    return all(min(getattr(a.Max, axis), getattr(b.Max, axis)) -
               max(getattr(a.Min, axis), getattr(b.Min, axis)) > tolerance for axis in "XYZ")


def part(brep, name="solid"):
    if not isinstance(brep, RG.Brep) or not brep.IsValid or not brep.IsSolid:
        raise ValueError("体积碰撞检查需要有效闭合 Brep：" + name)
    # Work on a private copy: boolean orientation must not depend on a caller's
    # inward-facing solid, and the source document must remain unchanged.
    brep = brep.DuplicateBrep()
    if brep.SolidOrientation == RG.BrepSolidOrientation.Inward:
        brep.Flip()
    if brep.SolidOrientation != RG.BrepSolidOrientation.Outward:
        raise ValueError("闭合实体方向不明：" + name)
    mesh = RG.Mesh()
    mesh.Vertices.UseDoublePrecisionVertices = True
    settings = RG.MeshingParameters()
    settings.Tolerance = 0.1
    settings.JaggedSeams = False
    settings.SimplePlanes = True
    for piece in RG.Mesh.CreateFromBrep(brep, settings) or []:
        mesh.Append(piece)
    if mesh.Faces.Count == 0 or not mesh.IsValid:
        raise ValueError("无法三角化：" + name)
    # A failed triangulation is not interchangeable with an empty mesh.
    if mesh.Faces.QuadCount and not mesh.Faces.ConvertQuadsToTriangles():
        raise ValueError("无法拆分三角面：" + name)
    mesh.Vertices.CombineIdentical(True, True)
    mesh.Weld(math.pi)
    mesh.UnifyNormals()
    if not mesh.Normals.ComputeNormals():
        raise ValueError("无法计算三角网格法向：" + name)
    if not mesh.IsValid or not mesh.IsSolid:
        raise ValueError("三角网格不是有效闭合有向流形：" + name)
    orientation = mesh.SolidOrientation()
    if orientation == -1:
        mesh.Flip(True, True, True)
    elif orientation != 1:
        raise ValueError("三角网格实体方向不明：" + name)
    return {"brep": brep, "mesh": mesh, "bbox": brep.GetBoundingBox(True), "name": name}


def unit(identifier, parts, guid="", role="component"):
    if not parts:
        raise ValueError("构件没有可检查的实体：" + identifier)
    bbox = RG.BoundingBox.Empty
    for item in parts:
        bbox.Union(item["bbox"])
    return {"id": identifier, "guid": guid, "role": role, "parts": parts, "bbox": bbox}


def _parameters(clearance, tolerance):
    if not math.isfinite(clearance) or clearance < 0:
        raise ValueError("净距须为有限非负毫米数")
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("布尔公差须为有限正毫米数")


def _mass(geometry, area=False):
    props = (RG.AreaMassProperties if area else RG.VolumeMassProperties).Compute(geometry)
    if props is None:
        raise InconclusiveGeometry("area unavailable" if area else "volume unavailable")
    try:
        value = abs(float(props.Area if area else props.Volume))
        center = props.Centroid
        if not math.isfinite(value) or value <= 0 or not center.IsValid:
            raise InconclusiveGeometry("invalid mass properties")
        return value, xyz(center)
    finally:
        props.Dispose()


def _box_proof(item):
    """Prove an axis-aligned box from its faces/edges/corners, not its bounds."""
    if "axis_box_proof" in item:
        return item["axis_box_proof"]
    brep, bbox = item["brep"], item["bbox"]
    epsilon = 1e-7
    proved = False
    bounds = [getattr(point, axis) for point in (bbox.Min, bbox.Max) for axis in "XYZ"]
    if (all(math.isfinite(value) for value in bounds)
            and all(getattr(bbox.Max, axis) > getattr(bbox.Min, axis) for axis in "XYZ")
            and brep.Vertices.Count == 8 and brep.Edges.Count == 12 and brep.Faces.Count == 6):
        corners = set()
        proved = True
        for vertex in brep.Vertices:
            code = []
            for axis in "XYZ":
                value = getattr(vertex.Location, axis)
                if abs(value - getattr(bbox.Min, axis)) <= epsilon:
                    code.append(0)
                elif abs(value - getattr(bbox.Max, axis)) <= epsilon:
                    code.append(1)
                else:
                    proved = False
            corners.add(tuple(code))
        proved = proved and len(corners) == 8 and all(len(code) == 3 for code in corners)
        for edge in brep.Edges:
            delta = edge.PointAtEnd - edge.PointAtStart
            if not edge.IsLinear(epsilon) or sum(abs(getattr(delta, axis)) > epsilon for axis in "XYZ") != 1:
                proved = False
        supports = set()
        for face in brep.Faces:
            edges = list(face.AdjacentEdges())
            if not face.IsPlanar(epsilon) or face.Loops.Count != 1 or len(edges) != 4:
                proved = False
                continue
            points = [brep.Edges[index].PointAtStart for index in edges]
            points += [brep.Edges[index].PointAtEnd for index in edges]
            support = [(axis, side) for axis in "XYZ" for side in (0, 1)
                       if all(abs(getattr(point, axis) - getattr(bbox.Min if side == 0 else bbox.Max, axis))
                              <= epsilon for point in points)]
            if len(support) != 1:
                proved = False
            else:
                supports.add(support[0])
        proved = proved and len(supports) == 6
    item["axis_box_proof"] = bool(proved)
    return bool(proved)


def _clearance_proof(a, b):
    """An AABB distance is exact only after both actual Breps prove to be boxes."""
    if not (_box_proof(a) and _box_proof(b)):
        return None
    value = gap(a["bbox"], b["bbox"])
    if not math.isfinite(value) or value < 0:
        raise InconclusiveGeometry("non-finite proved-box clearance")
    bounds = lambda box: [[float(getattr(point, axis)) for axis in "XYZ"]
                          for point in (box.Min, box.Max)]
    return {"actual_gap_mm": value, "distance_evidence": {
        "method": "proved_axis_aligned_box_gap", "axis_box_proof": [True, True],
        "a_bounds_mm": bounds(a["bbox"]), "b_bounds_mm": bounds(b["bbox"]),
        "comparison_epsilon_mm": CLEARANCE_EPSILON_MM}}


def _clearance_kind(value, clearance):
    if value <= CLEARANCE_EPSILON_MM:
        return "contact"
    if math.isclose(value, clearance, rel_tol=0.0, abs_tol=CLEARANCE_EPSILON_MM):
        return "at_clearance"
    return "below_clearance" if value < clearance else None


def _box_witness(a, b):
    """Midpoint of closest points of two proved boxes; model geometry, not a survey."""
    values = []
    for axis in "XYZ":
        lo = max(getattr(a.Min, axis), getattr(b.Min, axis))
        hi = min(getattr(a.Max, axis), getattr(b.Max, axis))
        values.append((lo + hi) / 2.0)
    return values


def _planar_mesh_proof(item):
    """Restrict the mesh fallback to checked, connected polygonal boundaries.

    Curved tessellation and disconnected nested shells cannot certify a solid
    relation from one boundary sample. They are deliberately not accepted here.
    """
    if "planar_mesh_proof" in item:
        return item["planar_mesh_proof"]
    brep, mesh = item["brep"], item["mesh"]
    result = {"ok": False}
    item["planar_mesh_proof"] = result
    try:
        epsilon = 1e-6
        if not all(face.IsPlanar(1e-7) for face in brep.Faces):
            raise InconclusiveGeometry("fallback does not certify curved faces")
        if not all(edge.IsLinear(1e-7) for edge in brep.Edges):
            raise InconclusiveGeometry("fallback does not certify curved edges")
        result.update(mesh_valid=bool(mesh.IsValid), mesh_solid=bool(mesh.IsSolid),
                      boundary_components=int(mesh.DisjointMeshCount))
        if not mesh.IsValid or not mesh.IsSolid or mesh.DisjointMeshCount != 1:
            raise InconclusiveGeometry("fallback requires one closed oriented manifold boundary")
        bv, _ = _mass(brep)
        mv, _ = _mass(mesh)
        ba, _ = _mass(brep, True)
        ma, _ = _mass(mesh, True)
        result.update(brep_volume=bv, mesh_volume=mv, brep_area=ba, mesh_area=ma)
        if abs(bv - mv) > max(1e-6, bv * 1e-8) or abs(ba - ma) > max(1e-6, ba * 1e-8):
            raise InconclusiveGeometry("fallback mesh area/volume does not match source Brep")
        # Ensure meshing did not bridge a trimmed opening or leave the source
        # planes. Mass equality adds a check against missing/duplicated regions.
        samples = list(mesh.Vertices.ToPoint3dArray())
        for face in mesh.Faces:
            a, b, c = (mesh.Vertices.Point3dAt(index) for index in (face.A, face.B, face.C))
            samples.extend([RG.Point3d((a.X + b.X + c.X) / 3, (a.Y + b.Y + c.Y) / 3,
                                      (a.Z + b.Z + c.Z) / 3),
                            RG.Point3d((a.X + b.X) / 2, (a.Y + b.Y) / 2, (a.Z + b.Z) / 2),
                            RG.Point3d((b.X + c.X) / 2, (b.Y + c.Y) / 2, (b.Z + c.Z) / 2),
                            RG.Point3d((c.X + a.X) / 2, (c.Y + a.Y) / 2, (c.Z + a.Z) / 2)])
        maximum = 0.0
        for point in samples:
            closest = brep.ClosestPoint(point)
            if not closest.IsValid:
                raise InconclusiveGeometry("Brep closest-point audit failed")
            maximum = max(maximum, point.DistanceTo(closest))
        result["max_boundary_error_mm"] = maximum
        if maximum > epsilon:
            raise InconclusiveGeometry("fallback mesh leaves the trimmed Brep boundary")
        result["ok"] = True
    except Exception as error:
        result["reason"] = str(error)
    return result


def _mesh_boundaries(a, b):
    """Call the modern API with an explicit success flag, including out args.

    Reflection avoids Python.NET overload/out-argument differences between the
    Rhino CPython and IronPython hosts. False is always an operation failure.
    """
    import clr
    import System
    from System.Collections.Generic import List
    from System.Threading import CancellationToken
    log = Rhino.FileIO.TextLog()
    try:
        methods = [method for method in clr.GetClrType(RG.Intersect.Intersection).GetMethods()
                   if method.Name == "MeshMesh" and len(method.GetParameters()) == 10]
        if len(methods) != 1:
            raise InconclusiveGeometry("explicit-success MeshMesh API unavailable")
        meshes = List[RG.Mesh]()
        meshes.Add(a)
        meshes.Add(b)
        arguments = System.Array[System.Object]([
            meshes, System.Double(1e-7), None, System.Boolean(True), None,
            System.Boolean(False), None, log, CancellationToken(False), None])
        success = bool(methods[0].Invoke(None, arguments))
        return {"success": success, "intersections": len(arguments[2]) if arguments[2] is not None else 0,
                "overlap_polylines": len(arguments[4]) if arguments[4] is not None else 0,
                "log": log.ToString()}
    except Exception as error:
        inner = getattr(error, "InnerException", None)
        return {"success": False, "intersections": 0, "overlap_polylines": 0,
                "error": str(inner if inner is not None else error), "log": log.ToString()}
    finally:
        log.Dispose()


def _brep_boundaries(a, b, tolerance):
    try:
        success, curves, points = RG.Intersect.Intersection.BrepBrep(a, b, tolerance)
        return {"success": bool(success), "curves": len(curves) if curves is not None else 0,
                "points": len(points) if points is not None else 0}
    except Exception as error:
        return {"success": False, "error": str(error)}


def _point_relation(a, b):
    epsilon = 1e-7
    pa, pb = a["brep"].Vertices[0].Location, b["brep"].Vertices[0].Location
    return {"a_sample_mm": xyz(pa), "b_sample_mm": xyz(pb),
            "a_in_b": bool(b["brep"].IsPointInside(pa, epsilon, True)),
            "b_in_a": bool(a["brep"].IsPointInside(pb, epsilon, True)),
            "a_in_b_inclusive": bool(b["brep"].IsPointInside(pa, epsilon, False)),
            "b_in_a_inclusive": bool(a["brep"].IsPointInside(pb, epsilon, False)),
            "a_in_b_mesh": bool(b["mesh"].IsPointInside(pa, epsilon, True)),
            "b_in_a_mesh": bool(a["mesh"].IsPointInside(pb, epsilon, True))}


def _fallback(a, b, trace):
    trace["axis_box_proof"] = [_box_proof(a), _box_proof(b)]
    if all(trace["axis_box_proof"]):
        lo = [max(getattr(a["bbox"].Min, axis), getattr(b["bbox"].Min, axis)) for axis in "XYZ"]
        hi = [min(getattr(a["bbox"].Max, axis), getattr(b["bbox"].Max, axis)) for axis in "XYZ"]
        widths = [max(0.0, hi[index] - lo[index]) for index in range(3)]
        trace["resolution"] = "proved axis-aligned box intersection"
        return widths[0] * widths[1] * widths[2], [(x + y) / 2 for x, y in zip(lo, hi)]
    trace["planar_mesh_proof"] = [_planar_mesh_proof(a), _planar_mesh_proof(b)]
    if not all(proof["ok"] for proof in trace["planar_mesh_proof"]):
        raise InconclusiveGeometry("empty/failed boolean; mesh fallback geometry not certified")
    boundary = _mesh_boundaries(a["mesh"], b["mesh"])
    trace["mesh_mesh"] = boundary
    if not boundary["success"]:
        raise InconclusiveGeometry("MeshMesh returned False (operation failed): " + boundary.get("error", "native API"))
    if boundary.get("log", "").strip():
        # The native API can return True while logging an invalid/discarded hit.
        # Such a result may show an intersection, but cannot prove its absence.
        raise InconclusiveGeometry("MeshMesh diagnostic prevents a conclusive empty-boundary proof")
    if boundary["intersections"] or boundary["overlap_polylines"]:
        raise InconclusiveGeometry("boolean has no solid result but actual boundaries intersect/overlap")
    brep_boundary = trace.get("brep_brep", {})
    if brep_boundary.get("success") and (brep_boundary.get("curves") or brep_boundary.get("points")):
        raise InconclusiveGeometry("successful BrepBrep and MeshMesh disagree on boundary intersection")
    relation = _point_relation(a, b)
    trace["point_containment"] = relation
    for prefix in ("a_in_b", "b_in_a"):
        if relation[prefix] != relation[prefix + "_mesh"] or relation[prefix] != relation[prefix + "_inclusive"]:
            raise InconclusiveGeometry("Brep/mesh containment inconsistent or sample lies on boundary")
    if relation["a_in_b"] and relation["b_in_a"]:
        raise InconclusiveGeometry("disjoint connected boundaries cannot contain each other")
    # With two certified connected closed boundaries and a successful empty
    # boundary intersection, each complete boundary lies on one side of the
    # other. A vertex is then sufficient; it is not sufficient on its own.
    if relation["a_in_b"] or relation["b_in_a"]:
        trace["resolution"] = "certified disjoint polygonal boundaries and containment"
        return _mass(a["brep"] if relation["a_in_b"] else b["brep"])
    trace["resolution"] = "certified disjoint polygonal boundaries, neither contained"
    return 0.0, None


def _intersection(a, b, tolerance, trace):
    try:
        result = RG.Brep.CreateBooleanIntersection(a["brep"], b["brep"], tolerance)
        trace["brep_boolean"] = {"is_null": result is None, "count": len(result) if result is not None else 0}
        if result is not None and len(result):
            volume, weighted = 0.0, [0.0, 0.0, 0.0]
            for solid in result:
                if not solid.IsValid or not solid.IsSolid:
                    raise InconclusiveGeometry("boolean returned an invalid/open Brep")
                value, center = _mass(solid)
                volume += value
                weighted = [weighted[index] + value * center[index] for index in range(3)]
            trace["resolution"] = "closed Brep boolean intersection"
            return volume, [value / volume for value in weighted]
    except Exception as error:
        trace["brep_boolean"] = dict(trace.get("brep_boolean", {}), error=str(error))
    trace["brep_brep"] = _brep_boundaries(a["brep"], b["brep"], tolerance)
    return _fallback(a, b, trace)


def check_pair(a, b, clearance=10.0, tolerance=0.01):
    _parameters(clearance, tolerance)
    hits, near, unresolved = [], [], []
    if gap(a["bbox"], b["bbox"]) > clearance + CLEARANCE_EPSILON_MM:
        return hits, near, unresolved
    for pa in a["parts"]:
        for pb in b["parts"]:
            if gap(pa["bbox"], pb["bbox"]) > clearance + CLEARANCE_EPSILON_MM:
                continue
            detail = {"a": a["id"], "b": b["id"], "a_guid": a["guid"], "b_guid": b["guid"],
                      "a_part": pa["name"], "b_part": pb["name"]}
            trace = {}
            try:
                if overlap(pa["bbox"], pb["bbox"], 0.0):
                    volume, center = _intersection(pa, pb, tolerance, trace)
                    if volume > tolerance ** 3:
                        hits.append(dict(detail, intersection_volume_mm3=round(volume, 6),
                                         point_mm=center, evidence=trace))
                        continue
                proof = _clearance_proof(pa, pb)
                mesh_proofs = [_planar_mesh_proof(pa), _planar_mesh_proof(pb)] if proof is None else None
                certified_mesh = mesh_proofs is not None and all(value["ok"] for value in mesh_proofs)
                search_distance = max(clearance, tolerance) + (2 * PLANAR_MESH_ERROR_MM if certified_mesh else 0.0)
                events = MeshClash.Search(pa["mesh"], pb["mesh"], search_distance, 1)
                if events is None:
                    raise InconclusiveGeometry("MeshClash returned null")
                kind = _clearance_kind(proof["actual_gap_mm"], clearance) if proof else "threshold_candidate"
                if kind is None or (not proof and not events and certified_mesh):
                    continue
                contact_search = MeshClash.Search(pa["mesh"], pb["mesh"], tolerance, 1)
                if contact_search is None:
                    raise InconclusiveGeometry("contact MeshClash returned null")
                row = dict(detail, kind=kind, required_clearance_mm=clearance,
                           mesh_witness_available=bool(events),
                           mesh_contact_search={"tolerance_mm": tolerance, "hit": bool(contact_search)})
                if proof:
                    row.update(proof)
                else:
                    row["review_required"] = True
                    row["distance_status"] = "mesh threshold candidate; exact model distance unverified"
                    row["clearance_mesh_proof"] = {"proofs": mesh_proofs, "search_distance_mm": search_distance,
                                                    "boundary_error_budget_mm": 2 * PLANAR_MESH_ERROR_MM}
                if events:
                    event = events[0]
                    if not event.ClashPoint.IsValid or not math.isfinite(float(event.ClashRadius)):
                        raise InconclusiveGeometry("invalid mesh-clearance witness")
                    row.update(point_mm=xyz(event.ClashPoint), witness_radius_mm=float(event.ClashRadius))
                else:
                    row["point_mm"] = _box_witness(pa["bbox"], pb["bbox"])
                    row["point_source"] = "proved_box_closest_midpoint" if proof else "unverified_bbox_candidate_midpoint"
                near.append(row)
            except Exception as error:
                unresolved.append(dict(detail, reason=str(error), evidence=trace))
    return hits, near, unresolved


def analyse(units, clearance=10.0, tolerance=0.01):
    _parameters(clearance, tolerance)
    ordered = sorted(units, key=lambda item: (item["bbox"].Min.X, item["id"]))
    active, clashes, nearby, unresolved = [], [], [], []
    candidates = 0
    for b in ordered:
        active = [a for a in active if a["bbox"].Max.X + clearance + CLEARANCE_EPSILON_MM >= b["bbox"].Min.X]
        for a in active:
            if gap(a["bbox"], b["bbox"]) > clearance + CLEARANCE_EPSILON_MM:
                continue
            candidates += 1
            hits, near, errors = check_pair(a, b, clearance, tolerance)
            clashes.extend(hits)
            nearby.extend(near)
            unresolved.extend(errors)
        active.append(b)
    blocking = [event for event in nearby if event["kind"] != "at_clearance"]
    return {"ok": not clashes and not unresolved, "clearance_ok": not blocking and not clashes and not unresolved,
            "units": len(units), "solid_parts": sum(len(u["parts"]) for u in units),
            "candidate_pairs": candidates, "clashes": clashes, "clearance_events": nearby,
            "unresolved": unresolved, "clearance_mm": clearance, "boolean_tolerance_mm": tolerance,
            "clearance_comparison_epsilon_mm": CLEARANCE_EPSILON_MM,
            "clearance_counts": {kind: sum(event["kind"] == kind for event in nearby)
                                 for kind in ("contact", "below_clearance", "at_clearance", "threshold_candidate")},
            "blocking_clearance_events": len(blocking),
            "method": "closed Brep boolean volume; proved-box or certified polygonal-boundary fallback; mesh threshold clearance",
            "scope": "between distinct units; internal designed connections within each unit are not checked",
            "clearance_semantics": "proved boxes have exact model gaps; at-threshold records do not block; contacts remain unapproved; non-box mesh witnesses require distance review, not an exact minimum-distance claim"}


def _fixture_cases():
    def box(x0, y0, z0, x1, y1, z1):
        return RG.Box(RG.BoundingBox(RG.Point3d(x0, y0, z0), RG.Point3d(x1, y1, z1))).ToBrep()
    a = unit("A", [part(box(0, 0, 0, 10, 10, 10))])
    overlap_unit = unit("overlap", [part(box(9, 0, 0, 19, 10, 10))])
    inside = unit("contained", [part(box(2, 2, 2, 3, 3, 3))])
    above = unit("above", [part(box(0, 0, 15, 10, 10, 25))])
    touching = unit("touching", [part(box(10, 0, 0, 20, 10, 10))])
    shell = RG.Brep.CreateBooleanDifference(box(0, 0, 0, 10, 10, 10), box(2, 2, -1, 8, 8, 11), 0.01)
    if shell is None or len(shell) != 1:
        raise InconclusiveGeometry("through-hole fixture BooleanDifference failed")
    cavity = unit("cavity", [part(shell[0])])
    probe = unit("in-void", [part(box(4, 4, 4, 6, 6, 6))])
    return [("100 mm3 overlap", a, overlap_unit, 5), ("full containment", a, inside, 1),
            ("3D separated projection", a, above, 6), ("clearance threshold", a, above, 4),
            ("contact without overlap", a, touching, 2),
            ("void inside overlapping bounding boxes", cavity, probe, 1)]


def _diagnose(a, b, clearance):
    pa, pb = a["parts"][0], b["parts"][0]
    row = {"a": a["id"], "b": b["id"], "clearance_mm": clearance,
           "bbox_gap_mm": gap(pa["bbox"], pb["bbox"]),
           "parts": [{"bbox_min_mm": xyz(item["bbox"].Min), "bbox_max_mm": xyz(item["bbox"].Max),
                       "volume_mm3": _mass(item["brep"])[0], "mesh_faces": item["mesh"].Faces.Count,
                       "axis_box_proof": _box_proof(item), "planar_mesh_proof": _planar_mesh_proof(item)}
                      for item in (pa, pb)]}
    try:
        result = RG.Brep.CreateBooleanIntersection(pa["brep"], pb["brep"], 0.01)
        row["brep_boolean"] = {"is_null": result is None, "count": len(result) if result is not None else 0,
                               "volumes_mm3": [_mass(solid)[0] for solid in result or []]}
    except Exception as error:
        row["brep_boolean"] = {"error": str(error)}
    row["brep_brep"] = _brep_boundaries(pa["brep"], pb["brep"], 0.01)
    try:
        row["mesh_mesh"] = _mesh_boundaries(pa["mesh"], pb["mesh"])
    except Exception as error:
        row["mesh_mesh"] = {"success": False, "error": str(error)}
    try:
        row["point_containment"] = _point_relation(pa, pb)
    except Exception as error:
        row["point_containment"] = {"error": str(error)}
    hits, near, errors = check_pair(a, b, clearance)
    row.update(clashes=hits, clearance_events=near, unresolved=errors)
    return row


def run_fixtures(diagnostics=None):
    cases = _fixture_cases()
    records = diagnostics if diagnostics is not None else []
    # Collect all native results before asserting, so a failed self-check still
    # leaves a useful six-case probe log instead of only the first exception.
    for name, a, b, clearance in cases:
        row = _diagnose(a, b, clearance)
        row["fixture"] = name
        records.append(row)
    for row in records[-6:]:
        assert row["mesh_mesh"]["success"], "explicit-success MeshMesh self-check failed: " + str(row)
        assert not row["unresolved"], "fixture unresolved: " + str(row)
    first, inside, above, far, contact, hole = records[-6:]
    assert first["mesh_mesh"]["intersections"] or first["mesh_mesh"]["overlap_polylines"], "MeshMesh missed the known boundary overlap"
    assert contact["mesh_mesh"]["intersections"] or contact["mesh_mesh"]["overlap_polylines"], "MeshMesh missed the known boundary contact"
    for row in (inside, above, far, hole):
        assert not row["mesh_mesh"]["intersections"] and not row["mesh_mesh"]["overlap_polylines"], "MeshMesh invented an intersection of disjoint boundaries"
    assert len(first["clashes"]) == 1 and abs(first["clashes"][0]["intersection_volume_mm3"] - 100) < 1e-6
    assert len(inside["clashes"]) == 1 and abs(inside["clashes"][0]["intersection_volume_mm3"] - 1) < 1e-6, "完全包含的实体也必须检出"
    assert not above["clashes"] and above["clearance_events"], "相同平面投影不能误报体积碰撞，但须检出 5 mm 净距"
    assert not far["clashes"] and not far["clearance_events"], "大于阈值的净距不应报警"
    assert not contact["clashes"] and contact["clearance_events"] and all(
        item["kind"] == "contact" for item in contact["clearance_events"])
    assert not hole["clashes"] and not hole["clearance_events"], "包络重叠且物体位于孔洞时不能误报"
    # Also exercise containment through the polygonal fallback, rather than
    # only the independently proved axis-aligned-box path above. This box is
    # wholly in the material of the perforated solid, beside its through hole.
    polygonal_outer = cases[-1][1]
    contained = unit("in-polygonal-material", [part(RG.Box(RG.BoundingBox(
        RG.Point3d(0.25, 0.25, 4), RG.Point3d(1.25, 1.25, 6))).ToBrep())])
    polygonal_check = _diagnose(polygonal_outer, contained, 1)
    inside["polygonal_containment_subcheck"] = polygonal_check
    assert not polygonal_check["unresolved"] and len(polygonal_check["clashes"]) == 1
    assert abs(polygonal_check["clashes"][0]["intersection_volume_mm3"] - 2) < 1e-6
    proof = {}
    volume, center = _fallback(polygonal_outer["parts"][0], contained["parts"][0], proof)
    polygonal_check["forced_fallback"] = {"volume_mm3": volume, "centroid_mm": center, "evidence": proof}
    assert abs(volume - 2) < 1e-6 and proof["resolution"] == "certified disjoint polygonal boundaries and containment"
    # Counterexample: a failed boundary API must never produce a no-clash result.
    original = globals()["_mesh_boundaries"]
    rejected, diagnostic_rejected = [], []
    try:
        globals()["_mesh_boundaries"] = lambda a, b: {"success": False, "intersections": 0, "overlap_polylines": 0}
        hits, near, rejected = check_pair(cases[-1][1], cases[-1][2], 1)
        assert not hits and not near and len(rejected) == 1, "MeshMesh False was silently accepted"
        assert "False" in rejected[0]["reason"], "API failure must retain the failed operation"
        globals()["_mesh_boundaries"] = lambda a, b: {
            "success": True, "intersections": 0, "overlap_polylines": 0,
            "log": "An intersection computed a wrong result. Hit is invalid."}
        hits, near, diagnostic_rejected = check_pair(cases[-1][1], cases[-1][2], 1)
        assert not hits and not near and len(diagnostic_rejected) == 1, "MeshMesh invalid-hit diagnostic was silently accepted"
        assert "diagnostic" in diagnostic_rejected[0]["reason"]
    finally:
        globals()["_mesh_boundaries"] = original
    boundary = []
    a, b = cases[2][1:3]
    for threshold, expected in ((6, "below_clearance"), (5, "at_clearance"), (4, None)):
        row = _diagnose(a, b, threshold)
        q = analyse([a, b], threshold)
        row.update(expected_kind=expected, aggregate_clearance_ok=q["clearance_ok"])
        assert not row["clashes"] and not row["unresolved"]
        if expected is None:
            assert not row["clearance_events"]
        else:
            assert len(row["clearance_events"]) == 1 and row["clearance_events"][0]["kind"] == expected
            assert abs(row["clearance_events"][0]["actual_gap_mm"] - 5) <= CLEARANCE_EPSILON_MM
        assert q["clearance_ok"] == (threshold <= 5), "equality must not be called insufficient clearance"
        boundary.append(row)
    almost = unit("9.999mm-separated", [part(RG.Box(RG.BoundingBox(
        RG.Point3d(0, 0, 19.999), RG.Point3d(10, 10, 29.999))).ToBrep())])
    narrow = _diagnose(a, almost, 10)
    narrow["aggregate_clearance_ok"] = analyse([a, almost], 10)["clearance_ok"]
    assert not narrow["clashes"] and not narrow["unresolved"] and not narrow["aggregate_clearance_ok"]
    assert len(narrow["clearance_events"]) == 1 and narrow["clearance_events"][0]["kind"] == "below_clearance"
    assert abs(narrow["clearance_events"][0]["actual_gap_mm"] - 9.999) <= CLEARANCE_EPSILON_MM
    candidate = _diagnose(cases[-1][1], cases[-1][2], 3)
    candidate["aggregate_clearance_ok"] = analyse([cases[-1][1], cases[-1][2]], 3)["clearance_ok"]
    assert not candidate["clashes"] and not candidate["unresolved"] and not candidate["aggregate_clearance_ok"]
    assert candidate["clearance_events"] and all(item["kind"] == "threshold_candidate"
        and item["review_required"] and "actual_gap_mm" not in item for item in candidate["clearance_events"])
    sphere = unit("curved-no-hit", [part(RG.Brep.CreateFromSphere(RG.Sphere(RG.Point3d(20, 20, 5), 10)))])
    curved = _diagnose(a, sphere, 1)
    curved["aggregate_clearance_ok"] = analyse([a, sphere], 1)["clearance_ok"]
    assert not curved["clashes"] and not curved["unresolved"] and not curved["aggregate_clearance_ok"]
    assert len(curved["clearance_events"]) == 1 and curved["clearance_events"][0]["kind"] == "threshold_candidate"
    assert curved["clearance_events"][0]["mesh_witness_available"] is False
    return {"ok": True, "checks": [case[0] for case in cases], "diagnostics": records,
            "boundary_checks": {"ok": True, "model_gap_mm": 5.0, "thresholds_mm": [6, 5, 4],
                                "diagnostics": boundary, "subthreshold_check": narrow,
                                "nonbox_candidate_check": candidate, "uncertified_nohit_check": curved},
            "failure_counterexample": {"ok": True, "unresolved": rejected,
                                       "invalid_hit_diagnostic_unresolved": diagnostic_rejected}}

"""Clearance equality, independent source proof, and attempts to wash a report green."""
import copy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import rhino3dm

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_quality import (EPSILON_MM, box_gap, source_box_bounds,
                                   verify_box_event_coverage, verify_clearance_event)


def event_for(distance, threshold, kind, witness=True):
    bounds = [[[0., 0., 0.], [10., 10., 10.]], [[0., 0., 10. + distance], [10., 10., 20. + distance]]]
    event = {"a": "A", "a_part": "part", "b": "B", "b_part": "part", "kind": kind,
             "required_clearance_mm": threshold, "actual_gap_mm": distance,
             "point_mm": [5., 5., 10. + distance / 2], "mesh_witness_available": witness,
             "mesh_contact_search": {"tolerance_mm": 0.01, "hit": distance <= 0.01},
             "distance_evidence": {"method": "proved_axis_aligned_box_gap", "axis_box_proof": [True, True],
                                   "a_bounds_mm": bounds[0], "b_bounds_mm": bounds[1], "comparison_epsilon_mm": EPSILON_MM}}
    if witness:
        event["witness_radius_mm"] = threshold / 2
    else:
        event["point_source"] = "proved_box_closest_midpoint"
    return event, bounds


class IndependentClearanceProof(unittest.TestCase):
    def test_actual_faces_edges_corners_prove_box_but_not_a_sphere(self):
        box = rhino3dm.Brep.CreateFromBoundingBox(rhino3dm.BoundingBox(rhino3dm.Point3d(0, 0, 0), rhino3dm.Point3d(10, 10, 10)))
        self.assertEqual(source_box_bounds(box), [[0., 0., 0.], [10., 10., 10.]])
        sphere = rhino3dm.Brep.CreateFromSphere(rhino3dm.Sphere(rhino3dm.Point3d(5, 5, 5), 5))
        self.assertIsNone(source_box_bounds(sphere))
        rotated = rhino3dm.Transform.Rotation(0.2, rhino3dm.Vector3d(0, 0, 1), rhino3dm.Point3d(0, 0, 0))
        self.assertIsNone(source_box_bounds(box, rotated))

    def test_five_mm_under_equal_and_over_threshold(self):
        for threshold, kind in ((6, "below_clearance"), (5, "at_clearance")):
            event, bounds = event_for(5, threshold, kind)
            self.assertEqual(verify_clearance_event(event, bounds, threshold, 0.01), kind)
        event, bounds = event_for(5, 4, "at_clearance")
        with self.assertRaisesRegex(AssertionError, "kind differs"):
            verify_clearance_event(event, bounds, 4, 0.01)

    def test_9_999_mm_is_insufficient_at_10_mm_and_boolean_tolerance_is_not_a_margin(self):
        event, bounds = event_for(9.999, 10, "below_clearance")
        self.assertEqual(verify_clearance_event(event, bounds, 10, 0.01), "below_clearance")
        event["kind"] = "at_clearance"
        with self.assertRaisesRegex(AssertionError, "kind differs"):
            verify_clearance_event(event, bounds, 10, 0.01)

    def test_positive_gap_is_not_contact_even_if_contact_mesh_search_hits(self):
        event, bounds = event_for(0.005, 1, "below_clearance")
        self.assertTrue(event["mesh_contact_search"]["hit"])
        self.assertEqual(verify_clearance_event(event, bounds, 1, 0.01), "below_clearance")

    def test_changed_gap_bounds_epsilon_and_nan_are_rejected(self):
        event, bounds = event_for(9.999, 10, "below_clearance")
        for change in (lambda e: e.update(actual_gap_mm=10), lambda e: e.update(actual_gap_mm=float("nan")),
                       lambda e: e["distance_evidence"].update(comparison_epsilon_mm=0.01),
                       lambda e: e["distance_evidence"]["b_bounds_mm"][0].__setitem__(2, 20)):
            altered = copy.deepcopy(event)
            change(altered)
            with self.subTest(altered=altered), self.assertRaises(AssertionError):
                verify_clearance_event(altered, bounds, 10, 0.01)

    def test_nonbox_cannot_claim_an_exact_gap_or_be_marked_at_threshold(self):
        event, _ = event_for(5, 5, "at_clearance")
        with self.assertRaisesRegex(AssertionError, "non-box"):
            verify_clearance_event(event, [None, None], 5, 0.01)
        event.update(kind="threshold_candidate", review_required=True)
        with self.assertRaisesRegex(AssertionError, "exact distance"):
            verify_clearance_event(event, [None, None], 5, 0.01)

    def test_analytic_midpoint_must_not_carry_a_mesh_radius(self):
        event, bounds = event_for(5, 5, "at_clearance", witness=False)
        self.assertEqual(verify_clearance_event(event, bounds, 5, 0.01), "at_clearance")
        event["witness_radius_mm"] = 2.5
        with self.assertRaisesRegex(AssertionError, "pretend"):
            verify_clearance_event(event, bounds, 5, 0.01)

    def test_deleting_or_duplicating_contact_is_rejected_by_independent_enumeration(self):
        event, bounds = event_for(0, 10, "contact")
        proven = {("A", "part"): bounds[0], ("B", "part"): bounds[1]}
        verify_box_event_coverage(proven, [event], 10)
        with self.assertRaisesRegex(AssertionError, "missing"):
            verify_box_event_coverage(proven, [], 10)
        with self.assertRaisesRegex(AssertionError, "duplicate"):
            verify_box_event_coverage(proven, [event, event], 10)
        far = dict(event, b="C")
        proven[("C", "part")] = [[0, 0, 100], [10, 10, 110]]
        with self.assertRaisesRegex(AssertionError, "outside source candidate"):
            verify_box_event_coverage(proven, [event], 10, [far])


class NativeClassificationWithoutRhino(unittest.TestCase):
    """Drive the real decision code with controlled native search responses, never start Rhino."""
    @classmethod
    def setUpClass(cls):
        rhino, geometry, intersection = (ModuleType(name) for name in ("Rhino", "Rhino.Geometry", "Rhino.Geometry.Intersect"))
        rhino.Geometry = geometry
        geometry.Intersect = intersection
        intersection.MeshClash = SimpleNamespace(Search=None)
        spec = importlib.util.spec_from_file_location("clearance_engine_test", ROOT / "rhino/spatial_quality.py")
        cls.engine = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"Rhino": rhino, "Rhino.Geometry": geometry, "Rhino.Geometry.Intersect": intersection}):
            spec.loader.exec_module(cls.engine)

    def run_pair(self, distance, threshold, *, box=True, certified=True, force_no_hit=False, mesh_distance=None):
        engine = self.engine
        point = lambda x, y, z: SimpleNamespace(X=x, Y=y, Z=z, IsValid=True)
        def unit(name, z):
            bbox = SimpleNamespace(Min=point(0, 0, z), Max=point(10, 10, z + 10))
            part = {"bbox": bbox, "mesh": name, "brep": None, "name": "part"}
            return {"id": name, "guid": name, "bbox": bbox, "parts": [part]}
        a, b = unit("A", 0), unit("B", 10 + distance)
        def search(left, right, limit, count):
            mesh_gap = distance if mesh_distance is None else mesh_distance
            return [] if force_no_hit or limit < mesh_gap else [SimpleNamespace(ClashPoint=point(5, 5, 10 + distance / 2), ClashRadius=limit / 2)]
        with patch.object(engine, "_box_proof", lambda item: box), patch.object(engine, "_planar_mesh_proof", lambda item: {"ok": certified}), patch.object(engine.MeshClash, "Search", search):
            return engine.analyse([a, b], threshold)

    def test_real_engine_equality_is_nonblocking_but_9_999_and_contact_block(self):
        for distance, threshold, kind, ok in ((5, 6, "below_clearance", False), (5, 5, "at_clearance", True),
                                              (5, 4, None, True), (9.999, 10, "below_clearance", False), (0, 10, "contact", False)):
            result = self.run_pair(distance, threshold)
            self.assertEqual(result["clearance_ok"], ok)
            self.assertEqual([e["kind"] for e in result["clearance_events"]], [kind] if kind else [])

    def test_nonbox_hit_and_uncertified_nohit_stay_blocking_without_exact_distance(self):
        for certified, no_hit in ((True, False), (False, True)):
            result = self.run_pair(5, 6, box=False, certified=certified, force_no_hit=no_hit)
            self.assertFalse(result["clearance_ok"])
            self.assertEqual(result["clearance_events"][0]["kind"], "threshold_candidate")
            self.assertNotIn("actual_gap_mm", result["clearance_events"][0])
            self.assertEqual(result["clearance_events"][0]["mesh_witness_available"], not no_hit)

    def test_certified_nohit_query_covers_both_boundary_error_budgets(self):
        # The candidate bounds remain within range. Each certified boundary can
        # contribute 1e-6 mm, so a search enlarged only once would miss this hit.
        result = self.run_pair(5, 6, box=False, certified=True, mesh_distance=6 + 1.5e-6)
        self.assertFalse(result["clearance_ok"])
        self.assertTrue(result["clearance_events"][0]["mesh_witness_available"])
        self.assertEqual(result["clearance_events"][0]["clearance_mesh_proof"]["boundary_error_budget_mm"], 2e-6)


if __name__ == "__main__":
    unittest.main()

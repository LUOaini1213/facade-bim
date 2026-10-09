"""真实分件/材料/正式任务的反例；错误必须被独立读回校验拒绝。"""
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ifcopenshell
import ifcopenshell.util.element as element
import rhino3dm

from scripts.check_ifc import (check_coping_semantics, check_part_geometry, check_tasks,
                               source_configuration, verify)
from scripts.export_ifc import _box_bounds


class DetailedIfc(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f = ifcopenshell.open(str(ROOT / "model" / "facade_bim.ifc"))
        cls.source = rhino3dm.File3dm.Read(str(ROOT / "model" / "facade_bim.3dm"))
        cls.source_panels = {obj.Attributes.GetUserString("pid"): obj for obj in cls.source.Objects
                             if isinstance(obj.Geometry, rhino3dm.InstanceReference) and obj.Attributes.GetUserString("pid")}
        cls.source_objects = {str(obj.Attributes.Id): obj for obj in cls.source.Objects}

    def coping(self):
        child = next(part for part in self.f.by_type("IfcBuildingElementPart") if part.Name == "S-RF-01/08 alu")
        metadata = element.get_pset(child, "FacadeBIM_Part")
        original = self.source_objects[metadata["RhinoObjectID"]]
        attrs = dict(self.source_panels[metadata["PanelID"]].Attributes.GetUserStrings())
        return child, original, attrs

    def test_coping_declares_actual_flat_sheet_mass_and_preserves_envelope(self):
        result = verify(self.f, self.source, geometry=False)
        self.assertEqual(result["coping_fabrication_parts"], 90)
        values = [element.get_pset(part, "FacadeBIM_Coping") for part in self.f.by_type("IfcBuildingElementPart")]
        coping = [props for props in values if props]
        self.assertEqual(len(coping), 90)
        _, original, _ = self.coping()
        box = original.Geometry.GetBoundingBox()
        record = source_configuration(ROOT / "model/facade_bim.3dm")
        thickness, density = record["configuration"]["COPING_MM"], record["configuration"]["ALU_DENSITY"]
        length, width, height = box.Max.X - box.Min.X, box.Max.Y - box.Min.Y, box.Max.Z - box.Min.Z
        self.assertEqual({(p["PhysicalThicknessMM"], p["FabricationLengthMM"], p["FabricationWidthMM"], p["EnvelopeHeightMM"])
                          for p in coping}, {(thickness, length, width, height)})
        self.assertAlmostEqual(sum(p["PhysicalMassKG"] for p in coping), length * width * thickness * density / 1e9 * 90, places=8)

    def test_coping_mass_cannot_be_rounded_or_derived_from_proxy_volume(self):
        child, original, attrs = self.coping()
        pset = self.f.by_id(element.get_pset(child, "FacadeBIM_Coping")["id"])
        prop = next(prop for prop in pset.HasProperties if prop.Name == "PhysicalMassKG")
        before = prop.NominalValue
        try:
            exact = before.wrappedValue
            rounded = round(exact, 3)
            # Some legal real-valued joints yield an exactly three-decimal
            # mass; in that case test a real perturbation rather than truth.
            rounded = rounded if abs(rounded - exact) > 1e-9 else exact + 0.001
            box = original.Geometry.GetBoundingBox()
            density = source_configuration(ROOT / "model/facade_bim.3dm")["configuration"]["ALU_DENSITY"]
            envelope_mass = (box.Max.X - box.Min.X) * (box.Max.Y - box.Min.Y) * (box.Max.Z - box.Min.Z) * density / 1e9
            for wrong in (rounded, envelope_mass):
                with self.subTest(mass=wrong):
                    prop.NominalValue = self.f.create_entity("IfcReal", wrong)
                    with self.assertRaisesRegex(AssertionError, "PhysicalMassKG"):
                        check_coping_semantics(child, original, attrs, source_configuration(ROOT / "model/facade_bim.3dm"))
        finally:
            prop.NominalValue = before

    def test_coping_proxy_thickness_and_manufacturing_length_corruption_are_rejected(self):
        child, original, attrs = self.coping()
        pset = self.f.by_id(element.get_pset(child, "FacadeBIM_Coping")["id"])
        for name, wrong in (("PhysicalThicknessMM", 50.0), ("FabricationLengthMM", 1600.0)):
            prop = next(prop for prop in pset.HasProperties if prop.Name == name)
            before = prop.NominalValue
            try:
                prop.NominalValue = self.f.create_entity("IfcReal", wrong)
                with self.assertRaisesRegex(AssertionError, name):
                    check_coping_semantics(child, original, attrs, source_configuration(ROOT / "model/facade_bim.3dm"))
            finally:
                prop.NominalValue = before

    def test_coping_role_cannot_hide_envelope_as_a_physical_solid(self):
        child, original, attrs = self.coping()
        pset = self.f.by_id(element.get_pset(child, "FacadeBIM_Part")["id"])
        prop = next(prop for prop in pset.HasProperties if prop.Name == "GeometryRepresentation")
        before = prop.NominalValue
        try:
            prop.NominalValue = self.f.create_entity("IfcLabel", "SOURCE_GEOMETRY")
            with self.assertRaisesRegex(AssertionError, "role/envelope"):
                check_coping_semantics(child, original, attrs, source_configuration(ROOT / "model/facade_bim.3dm"))
        finally:
            prop.NominalValue = before

    def test_parts_are_exactly_the_actual_block_objects(self):
        expected = Counter()
        definitions = {definition.Id: definition for definition in self.source.InstanceDefinitions}
        for panel in self.source_panels.values():
            for oid in definitions[panel.Geometry.ParentIdefId].GetObjectIds():
                expected[self.source_objects[str(oid)].Attributes.Name] += 1
        actual = Counter(part.ObjectType for part in self.f.by_type("IfcBuildingElementPart"))
        self.assertEqual(actual, expected)
        self.assertEqual(len(self.f.by_type("IfcMaterial")), 7)
        object_ids = {str(oid) for definition in definitions.values() for oid in definition.GetObjectIds()}
        expected_materials = {self.source.Materials[self.source_objects[oid].Attributes.MaterialIndex].Name for oid in object_ids}
        self.assertEqual({material.Name for material in self.f.by_type("IfcMaterial")},
                         expected_materials | {"混凝土"})

    def test_wrong_material_is_rejected(self):
        part = next(part for part in self.f.by_type("IfcBuildingElementPart") if part.ObjectType == "glass")
        relation = next(rel for rel in part.HasAssociations if rel.is_a("IfcRelAssociatesMaterial"))
        original = relation.RelatingMaterial
        try:
            relation.RelatingMaterial = next(material for material in self.f.by_type("IfcMaterial") if material.Name == "岩棉")
            with self.assertRaisesRegex(AssertionError, "part material differs"):
                verify(self.f, self.source, geometry=False)
        finally:
            relation.RelatingMaterial = original

    def test_valid_but_wrong_canonical_delivery_date_is_rejected(self):
        plate = self.f.by_type("IfcPlate")[0]
        pset = self.f.by_id(element.get_pset(plate, "FacadeBIM_Panel")["id"])
        prop = next(prop for prop in pset.HasProperties if prop.Name == "DeliveryDate")
        original = prop.NominalValue
        try:
            prop.NominalValue = self.f.create_entity("IfcLabel", "2026-12-31")
            with self.assertRaisesRegex(AssertionError, "canonical panel data differs"):
                verify(self.f, self.source, geometry=False)
        finally:
            prop.NominalValue = original

    def test_original_panel_quantities_are_preserved(self):
        mass_units = [unit for assignment in self.f.by_type("IfcUnitAssignment") for unit in assignment.Units
                      if getattr(unit, "UnitType", None) == "MASSUNIT"]
        self.assertEqual([(unit.Name, unit.Prefix) for unit in mass_units], [("GRAM", "KILO")])
        definitions = {definition.Id: definition for definition in self.source.InstanceDefinitions}
        for plate in self.f.by_type("IfcPlate"):
            panel = self.source_panels[plate.Name]
            attrs = panel.Attributes
            qto = element.get_pset(plate, "Qto_PlateBaseQuantities")
            first_id = definitions[panel.Geometry.ParentIdefId].GetObjectIds()[0]
            box = self.source_objects[str(first_id)].Geometry.GetBoundingBox()
            self.assertEqual(qto["Width"], box.Max.Y - box.Min.Y)
            self.assertEqual(qto["Perimeter"], 2.0 * (float(attrs.GetUserString("w_mm")) + float(attrs.GetUserString("h_mm"))))
            self.assertEqual(qto["GrossArea"], float(attrs.GetUserString("w_mm")) * float(attrs.GetUserString("h_mm")) / 1e6)
            self.assertEqual(qto["GrossWeight"], float(attrs.GetUserString("weight_kg")))

    def test_glass_shape_thickness_corruption_is_rejected(self):
        part = next(part for part in self.f.by_type("IfcBuildingElementPart") if part.ObjectType == "glass")
        source_part = self.source_objects[element.get_pset(part, "FacadeBIM_Part")["RhinoObjectID"]]
        solid = part.Representation.Representations[0].Items[0].MappingSource.MappedRepresentation.Items[0]
        original = solid.SweptArea.YDim
        try:
            solid.SweptArea.YDim -= 1.0
            with self.assertRaisesRegex(AssertionError, "geometry vertices differ"):
                check_part_geometry(self.f, part, source_part)
        finally:
            solid.SweptArea.YDim = original

    def test_missing_install_output_is_rejected(self):
        relation = self.f.by_type("IfcRelAssignsToProduct")[0]
        original = relation.RelatedObjects
        try:
            relation.RelatedObjects = [task for task in original if task.Identification.startswith("DELIVERY:")]
            with self.assertRaisesRegex(AssertionError, "task output association differs"):
                check_tasks(self.f, self.source_panels)
        finally:
            relation.RelatedObjects = original

    def test_wrong_task_date_is_rejected(self):
        task = next(task for task in self.f.by_type("IfcTask") if task.Identification.startswith("INSTALL:"))
        original = task.TaskTime.ScheduleStart
        try:
            task.TaskTime.ScheduleStart = (date.fromisoformat(original[:10]) + timedelta(days=1)).isoformat() + "T00:00:00"
            with self.assertRaisesRegex(AssertionError, "task day window differs"):
                check_tasks(self.f, self.source_panels)
        finally:
            task.TaskTime.ScheduleStart = original

    def test_missing_formal_task_time_is_rejected(self):
        task = self.f.by_type("IfcTask")[0]
        original = task.TaskTime
        try:
            task.TaskTime = None
            with self.assertRaisesRegex(AssertionError, "task time missing"):
                check_tasks(self.f, self.source_panels)
        finally:
            task.TaskTime = original

    def test_curved_geometry_is_not_silently_replaced_by_a_box(self):
        sphere = rhino3dm.Brep.CreateFromSphere(rhino3dm.Sphere(rhino3dm.Point3d(0, 0, 0), 100))
        with self.assertRaisesRegex(ValueError, "不是轴对齐长方体"):
            _box_bounds(sphere)


if __name__ == "__main__":
    unittest.main()

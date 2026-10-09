"""IDS must detect missing metadata/materials and invalid dates, not just accept the delivery."""
from pathlib import Path
import sys
import unittest
import ifcopenshell
import ifcopenshell.api.pset
import ifcopenshell.util.element as UE

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_ids import profile, validate_model


class DeliveryIDS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = profile()
        cls.source = (ROOT / "model" / cls.config["model"]).read_text(encoding="utf-8")
        cls.facade = cls.config["model"].startswith("facade")
        cls.pset = "FacadeBIM_Panel" if cls.facade else "BridgeBIM_Element"

    def model(self):
        return ifcopenshell.file.from_string(self.source)

    def target(self, model):
        return model.by_type("IfcPlate")[0] if self.facade else next(
            e for e in model.by_type("IfcBeam") if (e.Name or "").startswith("G-"))

    def edit(self, model, entity, name, value):
        pset = model.by_id(UE.get_pset(entity, self.pset)["id"])
        ifcopenshell.api.pset.edit_pset(model, pset=pset, properties={name: value})

    def test_delivery_passes_all_required_specifications(self):
        result = validate_model(self.model())
        self.assertTrue(result["status"])
        self.assertGreater(result["total_checks"], 1000)
        self.assertTrue(all(s["total_applicable"] > 0 for s in result["specifications"]))

    def test_missing_identity_is_detected(self):
        model = self.model()
        target = self.target(model)
        self.edit(model, target, "PanelID" if self.facade else "ElementID", None)
        result = validate_model(model)
        self.assertFalse(result["status"])
        self.assertGreater(result["total_checks_fail"], 0)

    def test_invalid_schedule_date_is_detected(self):
        model = self.model()
        self.edit(model, self.target(model), "InstallDate" if self.facade else "CastDate", "not-a-date")
        self.assertFalse(validate_model(model)["status"])

    def test_missing_actual_material_is_detected(self):
        model = self.model()
        target = model.by_type("IfcBuildingElementPart")[0] if self.facade else self.target(model)
        associations = list(target.HasAssociations)
        self.assertTrue(any(r.is_a("IfcRelAssociatesMaterial") for r in associations))
        for relation in associations:
            if relation.is_a("IfcRelAssociatesMaterial"):
                remaining = [e for e in relation.RelatedObjects if e != target]
                if remaining:
                    relation.RelatedObjects = remaining
                else:
                    model.remove(relation)
        self.assertIsNone(UE.get_material(target))
        self.assertFalse(validate_model(model)["status"])

    def test_empty_model_cannot_pass_vacuously(self):
        model = ifcopenshell.file(schema="IFC4" if self.facade else "IFC4X3_ADD2")
        self.assertFalse(validate_model(model)["status"])

    def test_missing_coping_role_cannot_escape_coping_applicability(self):
        model = self.model()
        coping = next(part for part in model.by_type("IfcBuildingElementPart") if part.Name == "S-RF-01/08 alu")
        ps = model.by_id(UE.get_pset(coping, "FacadeBIM_Part")["id"])
        ifcopenshell.api.pset.edit_pset(model, pset=ps, properties={"PartRole": None})
        result = validate_model(model)
        self.assertFalse(result["status"])
        specific = next(spec for spec in result["specifications"] if "coping" in spec["name"])
        self.assertEqual(specific["total_applicable"], 90)
        self.assertFalse(specific["status"])

    def test_negative_coping_physical_mass_is_rejected(self):
        model = self.model()
        coping = next(part for part in model.by_type("IfcBuildingElementPart") if part.Name == "S-RF-01/08 alu")
        ps = model.by_id(UE.get_pset(coping, "FacadeBIM_Coping")["id"])
        ifcopenshell.api.pset.edit_pset(model, pset=ps, properties={"PhysicalMassKG": -1.0})
        self.assertFalse(validate_model(model)["status"])


if __name__ == "__main__":
    unittest.main()

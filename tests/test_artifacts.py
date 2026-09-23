"""已提交的产物之间互相对得上：data/ ↔ 重算，.3dm ↔ data/，IFC ↔ .3dm。

CI 上没有 Rhino，所以 .3dm 用 rhino3dm（McNeel 的开源读写库）独立读，
IFC 用 ifcopenshell 读并交给它的几何引擎真的算出实体。
"""
import csv
import logging
import math
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ifcopenshell                    # noqa: E402
import ifcopenshell.geom               # noqa: E402
import ifcopenshell.util.element as UE  # noqa: E402
import ifcopenshell.util.unit as UU    # noqa: E402
import ifcopenshell.validate           # noqa: E402
import rhino3dm                        # noqa: E402

from facade.model import build_panels, footprint  # noqa: E402

DATA = os.path.join(ROOT, "data")
M3DM = os.path.join(ROOT, "model", "facade_bim.3dm")
MIFC = os.path.join(ROOT, "model", "facade_bim.ifc")


def rows(name):
    return list(csv.DictReader(open(os.path.join(DATA, name), encoding="utf-8")))


def run(script, *args):
    return subprocess.run([sys.executable, os.path.join(ROOT, "scripts", script)] + list(args),
                          capture_output=True, text=True, encoding="utf-8", env=dict(os.environ, PYTHONIOENCODING="utf-8"))


class DataIsReproducible(unittest.TestCase):
    def test_build_data_check(self):
        r = run("build_data.py", "--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class RhinoModelMatchesData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = rhino3dm.File3dm.Read(M3DM)
        cls.idefs = {d.Id: d.Name for d in cls.m.InstanceDefinitions}
        cls.inst = []
        for obj in cls.m.Objects:
            g = obj.Geometry
            if isinstance(g, rhino3dm.InstanceReference) and obj.Attributes.GetUserString("pid"):
                cls.inst.append((obj.Attributes, g))
        cls.data = {r["pid"]: r for r in rows("panels.csv")}

    def test_five_block_definitions_one_per_panel_type(self):
        self.assertEqual(sorted(self.idefs.values()), ["U1", "U2", "U3", "U4", "U5"])

    def test_one_instance_per_panel(self):
        pids = [a.GetUserString("pid") for a, _ in self.inst]
        self.assertEqual(len(pids), len(set(pids)))
        self.assertEqual(set(pids), set(self.data))

    def test_every_attribute_on_every_instance_equals_the_schedule(self):
        bad = []
        for a, _ in self.inst:
            row = self.data[a.GetUserString("pid")]
            for k, v in row.items():
                if a.GetUserString(k) != v:
                    bad.append((row["pid"], k, a.GetUserString(k), v))
        self.assertEqual(bad, [], bad[:5])
        self.assertEqual(len(self.data[next(iter(self.data))]), 26)

    def test_instance_block_matches_the_type_attribute(self):
        for a, g in self.inst:
            self.assertEqual(self.idefs[g.ParentIdefId], a.GetUserString("type"))

    def test_instance_transform_puts_the_panel_where_the_schedule_says(self):
        worst = 0.0
        for a, g in self.inst:
            row = self.data[a.GetUserString("pid")]
            xf = g.Xform
            d = max(abs(xf.M03 - float(row["origin_x"])), abs(xf.M13 - float(row["origin_y"])),
                    abs(xf.M23 - float(row["origin_z"])))
            worst = max(worst, d)
            rot = int(round(math.degrees(math.atan2(xf.M10, xf.M00)))) % 360
            self.assertEqual(rot, int(row["rot"]), row["pid"])
        self.assertLess(worst, 0.01)

    def test_no_local_paths_in_the_3dm(self):
        """.3dm 默认会写进本机存盘路径（UTF-16）和渲染环境里贴图的本机路径；公开仓库里不该有这些。
        允许的只有 Windows 的公共目录（C:/Users/Public）：构建脚本故意先存到那里再复制进仓库。
        奇偶两种字节对齐都按 UTF-16 解一遍，免得路径恰好从奇数字节开始时漏掉。"""
        b = open(M3DM, "rb").read()
        found = []
        for enc, text in (("utf-8", b.decode("utf-8", "ignore")), ("utf-16-le", b.decode("utf-16-le", "ignore")),
                          ("utf-16-le 错一字节", b[1:].decode("utf-16-le", "ignore"))):
            for word in ("AppData", "Desktop"):
                found += ["%s：%r" % (enc, text[max(0, m.start() - 40):m.start() + 40])
                          for m in re.finditer(word, text)]
            found += ["%s：%r" % (enc, text[m.start():m.start() + 80])
                      for m in re.finditer(r"[A-Za-z]:[\\/]Users[\\/]([^\\/\x00]+)", text) if m.group(1) != "Public"]
        self.assertEqual(len(found), 0, "%d 处本机路径，前几处：\n%s" % (len(found), "\n".join(found[:6])))


class IfcMatchesRhinoModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f = ifcopenshell.open(MIFC)
        cls.data = {r["pid"]: r for r in rows("panels.csv")}

    def test_ifc_is_exactly_what_the_committed_3dm_exports_to(self):
        r = run("export_ifc.py", "--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_schema_validation_reports_nothing(self):
        issues = []

        class Collect(logging.Handler):
            def emit(self, rec):
                issues.append(rec.getMessage())
        lg = logging.getLogger("ifc-validate")
        lg.addHandler(Collect())
        lg.setLevel(logging.DEBUG)
        ifcopenshell.validate.validate(self.f, lg, express_rules=False)
        self.assertEqual(issues, [])

    def test_entity_counts(self):
        counts = {c: len(self.f.by_type(c)) for c in
                  ("IfcPlate", "IfcPlateType", "IfcCurtainWall", "IfcBuildingStorey", "IfcMember", "IfcSlab")}
        self.assertEqual(counts, {"IfcPlate": 810, "IfcPlateType": 5, "IfcCurtainWall": 36,
                                  "IfcBuildingStorey": 9, "IfcMember": 4, "IfcSlab": 9})

    def test_each_plate_carries_its_panel_data_and_sits_in_the_right_storey(self):
        for plate in self.f.by_type("IfcPlate"):
            row = self.data[plate.Name]
            ps = UE.get_pset(plate, "FacadeBIM_Panel")
            self.assertEqual((ps["PanelID"], ps["PanelType"], ps["InstallDate"], ps["Stillage"]),
                             (row["pid"], row["type"], row["install_date"], row["stillage"]))
            self.assertEqual(UE.get_type(plate).Name, row["type"])
            cw = UE.get_aggregate(plate)
            self.assertEqual(cw.Name, "%s-%s" % (row["elev"], row["level"]))
            self.assertEqual(UE.get_container(cw).Name, row["level"])

    def test_quantities_add_up_to_the_takeoff(self):
        total = next(r for r in rows("takeoff.csv") if r["type"] == "合计")
        w = sum(UE.get_pset(p, "Qto_PlateBaseQuantities")["GrossWeight"] for p in self.f.by_type("IfcPlate"))
        self.assertAlmostEqual(w / 1000.0, float(total["weight_t"]), places=2)

    def test_geometry_engine_places_every_plate_on_the_model_envelope(self):
        """把 IFC 交给 ifcopenshell 的几何引擎算出实体，逐块比对世界坐标包络。"""
        st = ifcopenshell.geom.settings()
        st.set("use-world-coords", True)
        scale = 1.0 / UU.calculate_unit_scale(self.f)       # 引擎输出米，换回毫米
        exp = {p.pid: p for p in build_panels()}
        worst = 0.0
        for plate in self.f.by_type("IfcPlate"):
            sh = ifcopenshell.geom.create_shape(st, plate)   # 留住 sh：链式取 verts 会读到已释放的内存
            v = list(sh.geometry.verts)
            got = [c * scale for c in (min(v[0::3]), min(v[1::3]), min(v[2::3]),
                                       max(v[0::3]), max(v[1::3]), max(v[2::3]))]
            p = exp[plate.Name]
            x0, y0, x1, y1 = footprint(p)
            want = (x0, y0, p.origin[2], x1, y1, p.origin[2] + p.h)
            worst = max(worst, max(abs(a - b) for a, b in zip(got, want)))
        self.assertLess(worst, 0.01)


if __name__ == "__main__":
    unittest.main()

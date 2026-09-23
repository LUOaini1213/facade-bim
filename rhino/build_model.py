#! python3
"""在 Rhino 8 里建幕墙 BIM 模型，存 .3dm，并出图。

由 scripts/run_rhino.py 用 `Rhino.exe /runscript` 调起；仓库根目录经环境变量
FACADE_BIM_ROOT 传入（Rhino 命令行对非 ASCII 路径不可靠，所以脚本本身会被
复制到一个纯 ASCII 的临时路径再跑）。

模型组织：
- 5 种板块 = 5 个块定义（类型），810 块板 = 810 个块实例（构件）；
- 每个实例的名称 = 板块编号，属性里挂 BIM 数据（UserText）；
- 图层：结构 / 幕墙（按类型）/ 分析（按类型、按安装周着色的覆盖面）/ 标注 / 施工总平面 / 型录。
"""
import json
import os
import sys
import time
import traceback

ROOT = os.environ.get("FACADE_BIM_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import System                                          # noqa: E402
import System.Drawing as SD                            # noqa: E402
import System.Drawing.Imaging                          # noqa: E402,F401  PNG 编码器所在命名空间
import Rhino                                           # noqa: E402
import Rhino.Geometry as RG                            # noqa: E402
from System.Collections.Generic import List            # noqa: E402

from facade import config as C, site                   # noqa: E402
from facade.model import OUT_X, OUT_Y, TYPE_NAMES, corner_posts, to_world, zones_for  # noqa: E402
from facade.pipeline import compute, panel_rows        # noqa: E402

LOG = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": []}
DOC = Rhino.RhinoDoc.ActiveDoc
TYPE_COLORS = {"U1": (66, 133, 244), "U2": (120, 170, 220), "U3": (240, 160, 40),
               "U4": (46, 170, 120), "U5": (150, 110, 190)}
WEEK_COLORS = [(254, 229, 217), (252, 187, 161), (252, 146, 114), (251, 106, 74),
               (239, 59, 44), (203, 24, 29), (153, 0, 13), (103, 0, 13)]


def step(msg):
    LOG["steps"].append("%s  %s" % (time.strftime("%H:%M:%S"), msg))


# ------------------------------------------------------------------ 图层与材质
_layers = {}


def layer(path, rgb=(0, 0, 0)):
    if path in _layers:
        return _layers[path]
    parent_id = System.Guid.Empty
    if "::" in path:
        parent_id = DOC.Layers[layer(path.rsplit("::", 1)[0])].Id
    lay = Rhino.DocObjects.Layer()
    lay.Name = path.rsplit("::", 1)[-1]
    lay.Color = SD.Color.FromArgb(*rgb)
    if parent_id != System.Guid.Empty:
        lay.ParentLayerId = parent_id
    idx = DOC.Layers.Add(lay)
    if idx < 0:
        raise RuntimeError("图层创建失败：" + path)
    _layers[path] = idx
    return idx


def set_visible(path, on):
    lay = DOC.Layers[_layers[path]]
    lay.IsVisible = on
    try:
        lay.SetPersistentVisibility(on)
    except Exception:
        pass


_mats = {}


def material(name, rgb, transparency=0.0):
    if name not in _mats:
        m = Rhino.DocObjects.Material()
        m.Name = name
        m.DiffuseColor = SD.Color.FromArgb(*rgb)
        m.Transparency = transparency
        _mats[name] = DOC.Materials.Add(m)
    return _mats[name]


def attrs(layer_path, rgb=None, mat=None, name=None):
    a = Rhino.DocObjects.ObjectAttributes()
    a.LayerIndex = layer(layer_path)
    if rgb is not None:
        a.ObjectColor = SD.Color.FromArgb(*rgb)
        a.ColorSource = Rhino.DocObjects.ObjectColorSource.ColorFromObject
    if mat is not None:
        a.MaterialIndex = mat
        a.MaterialSource = Rhino.DocObjects.ObjectMaterialSource.MaterialFromObject
    if name:
        a.Name = name
    return a


def quad(p0, p1, p2, p3):
    """一张四边形网格。分析图层用网格不用 Brep：显示一样，文件小一个量级。"""
    m = RG.Mesh()
    for q in (p0, p1, p2, p3):
        m.Vertices.Add(q[0], q[1], q[2])
    m.Faces.AddFace(0, 1, 2, 3)
    m.Normals.ComputeNormals()
    m.Compact()
    return m


def box(x0, y0, z0, x1, y1, z1):
    return RG.Box(RG.BoundingBox(RG.Point3d(x0, y0, z0), RG.Point3d(x1, y1, z1))).ToBrep()


# ------------------------------------------------------------------ 块定义（板块类型）
PARTS = {
    "alu": ("铝型材", (178, 182, 188), 0.0),
    "glass": ("中空玻璃", (140, 190, 220), 0.55),
    "spandrel": ("层间釉面玻璃", (70, 78, 88), 0.0),
    "insulation": ("岩棉", (214, 196, 150), 0.0),
    "louver": ("铝百叶", (150, 150, 150), 0.0),
    "door": ("门扇玻璃", (120, 200, 190), 0.45),
}


def panel_parts(ptype, w, h):
    """一块板的零件 [(零件类, Brep)]，局部坐标。"""
    parts = []
    mw, d = C.MULLION_W, C.DEPTH
    parts.append(("alu", box(0, 0, 0, mw, d, h)))
    parts.append(("alu", box(w - mw, 0, 0, w, d, h)))
    z = 0
    for kind, zh in zones_for(ptype, h):
        z0, z1 = z, z + zh
        if kind == "transom":
            parts.append(("alu", box(mw, 0, z0, w - mw, d * 0.8, z1)))
        elif kind == "vision":
            parts.append(("glass", box(mw, 40, z0, w - mw, 64, z1)))
        elif kind == "door":
            parts.append(("door", box(mw + 10, 40, z0, w - mw - 10, 52, z1)))
        elif kind == "spandrel":
            parts.append(("spandrel", box(mw, 40, z0, w - mw, 46, z1)))
            parts.append(("insulation", box(mw, 100, z0, w - mw, 150, z1)))
            parts.append(("alu", box(mw, 150, z0, w - mw, 153, z1)))
        elif kind == "louver":
            zz = z0 + 30
            while zz + 20 <= z1:
                parts.append(("louver", box(mw, 20, zz, w - mw, 140, zz + 20)))
                zz += 100
        z = z1
    if ptype == "U5":
        parts.append(("alu", box(-C.JOINT / 2.0, -40, h, w + C.JOINT / 2.0, C.COPING_W - 40, h + 50)))
    return parts


def make_block_defs(panels):
    defs, sizes = {}, {}
    for p in panels:
        sizes.setdefault(p.ptype, (p.w, p.h))
    for ptype in sorted(sizes):
        w, h = sizes[ptype]
        geo = List[RG.GeometryBase]()
        att = List[Rhino.DocObjects.ObjectAttributes]()
        for part, brep in panel_parts(ptype, w, h):
            name, rgb, tr = PARTS[part]
            a = Rhino.DocObjects.ObjectAttributes()
            a.ObjectColor = SD.Color.FromArgb(*rgb)
            a.ColorSource = Rhino.DocObjects.ObjectColorSource.ColorFromObject
            a.MaterialIndex = material(name, rgb, tr)
            a.MaterialSource = Rhino.DocObjects.ObjectMaterialSource.MaterialFromObject
            a.Name = part
            geo.Add(brep)
            att.Add(a)
        desc = "%s %s，制作尺寸 %d×%d mm" % (ptype, TYPE_NAMES[ptype], w, h)
        idx = DOC.InstanceDefinitions.Add(ptype, desc, RG.Point3d.Origin, geo, att)
        if idx < 0:
            raise RuntimeError("块定义 %s 创建失败" % ptype)
        defs[ptype] = idx
    return defs


def xform(origin, rot):
    r = RG.Transform.Rotation(System.Math.PI * rot / 180.0, RG.Vector3d.ZAxis, RG.Point3d.Origin)
    t = RG.Transform.Translation(RG.Vector3d(origin[0], origin[1], origin[2]))
    return t * r


def _w(p, lx, ly, lz):
    return to_world(p.origin, p.rot, lx, ly, lz)


# ------------------------------------------------------------------ 模型主体
def build(r):
    panels = r["panels"]
    rows = {row["pid"]: row for row in panel_rows(r)}
    defs = make_block_defs(panels)
    step("块定义 %d 个" % len(defs))

    for t, rgb in TYPE_COLORS.items():
        layer("幕墙::%s %s" % (t, TYPE_NAMES[t]), rgb)
    n_keys = 0
    for p in panels:
        a = attrs("幕墙::%s %s" % (p.ptype, TYPE_NAMES[p.ptype]), name=p.pid)
        for k, v in rows[p.pid].items():
            a.SetUserString(k, str(v))
        n_keys = len(rows[p.pid])
        gid = DOC.Objects.AddInstanceObject(defs[p.ptype], xform(p.origin, p.rot), a)
        if gid == System.Guid.Empty:
            raise RuntimeError("块实例创建失败：" + p.pid)
    step("块实例 %d 个（每个挂 %d 项属性）" % (len(panels), n_keys))

    alu = material(*PARTS["alu"])
    for x, y, s, top in corner_posts():
        DOC.Objects.AddBrep(box(x, y, 0, x + s, y + s, top), attrs("幕墙::转角立柱", (178, 182, 188), alu))
    e = C.DEPTH + C.SLAB_GAP
    slab_mat = material("混凝土", (205, 205, 200))
    for lvl, z, _ in C.LEVELS:
        DOC.Objects.AddBrep(box(e, e, z - 250, OUT_X - e, OUT_Y - e, z),
                            attrs("结构::楼板", (205, 205, 200), slab_mat, name="楼板 " + lvl))
    step("转角立柱 4 根、楼板 %d 块" % len(C.LEVELS))

    wk0 = C.INSTALL_START
    for p in panels:
        pts = [_w(p, lx, -30, lz) for lx, lz in ((0, 0), (p.w, 0), (p.w, p.h), (0, p.h))]
        DOC.Objects.AddMesh(quad(*pts), attrs("分析::按板块类型", TYPE_COLORS[p.ptype], name=p.pid))
        week = (r["install"][p.pid][1] - wk0).days // 7
        DOC.Objects.AddMesh(quad(*pts), attrs("分析::按安装周", WEEK_COLORS[min(week, 7)], name=p.pid))
    step("分析覆盖面 %d 张" % (2 * len(panels)))

    for p in panels:
        if p.elev == "S" and p.level in ("L1", "L2", "L3") and p.col <= 6:
            c = _w(p, p.w / 2.0, -60, p.h / 2.0)
            DOC.Objects.AddTextDot(p.pid, RG.Point3d(*c), attrs("标注::板块编号", (40, 40, 40)))

    draw_site(r)
    draw_catalog(defs)
    draw_unfolded(r)


def _rect(x0, y0, x1, y1, z=0.0):
    pl = RG.Polyline()
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)):
        pl.Add(RG.Point3d(x, y, z))
    return pl


def _fill(x0, y0, x1, y1, z=0.0):
    return quad((x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z))


def draw_site(r):
    L = "施工总平面::"
    sx0, sy0, sx1, sy1 = C.SITE
    DOC.Objects.AddPolyline(_rect(sx0, sy0, sx1, sy1), attrs(L + "场地红线", (220, 30, 30)))
    DOC.Objects.AddMesh(_fill(sx0, sy0, sx1, sy1, -300), attrs(L + "场地", (236, 233, 224)))
    ex = C.EXCLUSION
    DOC.Objects.AddPolyline(_rect(-ex, -ex, OUT_X + ex, OUT_Y + ex, 5), attrs(L + "坠物禁区", (230, 120, 0)))
    for (x0, y0, x1, y1) in site.slots():
        DOC.Objects.AddMesh(_fill(x0 + 150, y0 + 150, x1 - 150, y1 - 150, 10), attrs(L + "堆场架位", (250, 200, 90)))
        DOC.Objects.AddPolyline(_rect(x0, y0, x1, y1, 12), attrs(L + "堆场架位", (160, 110, 20)))
    lx0, ly0, lx1, ly1 = site.laydown_bbox()
    DOC.Objects.AddTextDot("运输架堆场 %d 个架位（排程峰值 %d 架）" % (len(site.slots()), r["summary"]["peak_stock"]),
                           RG.Point3d((lx0 + lx1) / 2.0, ly0 - 2500, 20), attrs(L + "文字", (0, 0, 0)))
    cx, cy = C.CRANE
    DOC.Objects.AddCircle(RG.Circle(RG.Plane(RG.Point3d(cx, cy, 20), RG.Vector3d.ZAxis), C.CRANE_RADIUS),
                          attrs(L + "塔吊", (30, 90, 200)))
    DOC.Objects.AddBrep(box(cx - 1000, cy - 1000, 0, cx + 1000, cy + 1000, 45000),
                        attrs(L + "塔吊", (30, 90, 200), material("塔吊", (70, 110, 170))))
    DOC.Objects.AddTextDot("塔吊 R=%.0f m" % (C.CRANE_RADIUS / 1000.0), RG.Point3d(cx, cy - 3500, 20),
                           attrs(L + "文字", (0, 0, 0)))
    route = RG.Polyline()
    for x, y in C.TRUCK_ROUTE:
        route.Add(RG.Point3d(x, y, 15))
    DOC.Objects.AddPolyline(route, attrs(L + "车道", (60, 60, 60)))
    for (a, b) in C.GATES:
        DOC.Objects.AddLine(RG.Line(RG.Point3d(a[0], a[1], 20), RG.Point3d(b[0], b[1], 20)),
                            attrs(L + "大门", (0, 150, 60)))
    DOC.Objects.AddTextDot("入口", RG.Point3d(10000, -31500, 20), attrs(L + "文字", (0, 0, 0)))
    DOC.Objects.AddTextDot("出口", RG.Point3d(55000, -31500, 20), attrs(L + "文字", (0, 0, 0)))
    DOC.Objects.AddTextDot("坠物禁区 %.0f m" % (ex / 1000.0), RG.Point3d(OUT_X / 2.0, -ex - 1200, 20),
                           attrs(L + "文字", (0, 0, 0)))
    n = RG.Point3d(sx1 - 6000, sy1 - 9000, 20)
    DOC.Objects.AddLine(RG.Line(n, RG.Point3d(n.X, n.Y + 4000, 20)), attrs(L + "文字", (0, 0, 0)))
    DOC.Objects.AddTextDot("N", RG.Point3d(n.X, n.Y + 5000, 20), attrs(L + "文字", (0, 0, 0)))
    step("施工总平面：红线、禁区、%d 个架位、塔吊、车道、大门" % len(site.slots()))


UNFOLD_Y = -120000


def face_starts():
    """展开立面：南→东→北→西摊平成一条，面与面之间留出转角立柱宽。"""
    starts, s = {}, C.CORNER
    for elev, ncol in C.ELEVATIONS:
        starts[elev] = s
        s += ncol * C.MODULE + C.CORNER
    return starts, s


def _vsq(x0, z0, x1, z1, y):
    return quad((x0, y, z0), (x1, y, z0), (x1, y, z1), (x0, y, z1))


def unfolded_bbox():
    """两条展开立面加标签、图例的总包络（x0, z0, x1, z1），出图时按它取景。"""
    _, total = face_starts()
    top = C.LEVELS[-1][1] + C.LEVELS[-1][2]
    return (-7000, -5000, total + 52000, UNFOLD_GAP + top + 6000)


UNFOLD_GAP = 50000   # 两条展开立面的竖向间距（mm）


def draw_unfolded(r):
    """展开立面：上条按板块类型，下条按安装周（4D）。标签与图例画在同一竖直平面里。"""
    starts, total = face_starts()
    top = C.LEVELS[-1][1] + C.LEVELS[-1][2]
    y = UNFOLD_Y
    days = sorted(set(d for _, d in r["install"].values()))
    weeks = {}
    for d in days:
        weeks.setdefault((d - C.INSTALL_START).days // 7, []).append(d)
    counts = {}
    for p in r["panels"]:
        counts[p.ptype] = counts.get(p.ptype, 0) + 1
    names = {"S": "南立面 S", "E": "东立面 E", "N": "北立面 N", "W": "西立面 W"}
    L = "分析::展开立面"
    for dz, key, title in ((UNFOLD_GAP, "type", "展开立面 · 按板块类型"),
                           (0, "week", "展开立面 · 按安装周（4D）")):
        for p in r["panels"]:
            x0 = starts[p.elev] + (p.col - 1) * C.MODULE + C.JOINT / 2.0
            z0 = p.origin[2] + dz
            if key == "type":
                rgb = TYPE_COLORS[p.ptype]
            else:
                rgb = WEEK_COLORS[min((r["install"][p.pid][1] - C.INSTALL_START).days // 7, 7)]
            DOC.Objects.AddMesh(_vsq(x0, z0, x0 + p.w, z0 + p.h, y), attrs(L, rgb, name=p.pid))
        DOC.Objects.AddTextDot(title, RG.Point3d(total / 2.0, y - 50, dz + top + 3200), attrs(L, (0, 0, 0)))
        for elev, ncol in C.ELEVATIONS:
            DOC.Objects.AddTextDot("%s（%d 列）" % (names[elev], ncol),
                                   RG.Point3d(starts[elev] + ncol * C.MODULE / 2.0, y - 50, dz - 2800),
                                   attrs(L, (0, 0, 0)))
        for lvl, z, h in C.LEVELS:
            if lvl in ("L1", "L4", "L8", "RF"):
                DOC.Objects.AddTextDot(lvl, RG.Point3d(-3500, y - 50, dz + z + h / 2.0), attrs(L, (0, 0, 0)))
        if key == "type":
            items = [(TYPE_COLORS[t], "%s %s ×%d" % (t, TYPE_NAMES[t], counts[t])) for t in sorted(counts)]
        else:
            items = [(WEEK_COLORS[min(k, 7)], "第 %d 周 %s–%s" % (k + 1, ds[0].strftime("%m-%d"),
                      ds[-1].strftime("%m-%d"))) for k, ds in sorted(weeks.items())]
        sq = 3000
        pitch = min(4600, (top - 1000) / max(1, len(items) - 1)) if len(items) > 1 else 0
        lx = total + 5000
        for i, (rgb, label) in enumerate(items):
            zc = dz + top - 1500 - i * pitch
            DOC.Objects.AddMesh(_vsq(lx, zc - sq / 2.0, lx + sq, zc + sq / 2.0, y), attrs(L, rgb))
            DOC.Objects.AddTextDot(label, RG.Point3d(lx + sq + 17000, y - 50, zc), attrs(L, (0, 0, 0)))
    step("展开立面 2 条（按类型、按安装周），含图例")


CATALOG_Y = -60000


def draw_catalog(defs):
    x = 0
    for ptype in sorted(defs):
        DOC.Objects.AddInstanceObject(defs[ptype], xform((x, CATALOG_Y, 0), 0), attrs("型录", name="型录 " + ptype))
        DOC.Objects.AddTextDot("%s %s" % (ptype, TYPE_NAMES[ptype]), RG.Point3d(x + 790, CATALOG_Y - 1500, -300),
                               attrs("型录", (0, 0, 0)))
        x += 3000


# ------------------------------------------------------------------ 出图
def display_mode(*names):
    modes = Rhino.Display.DisplayModeDescription.GetDisplayModes()
    for n in names:
        for m in modes:
            if m.EnglishName == n:
                return m
    return None


def capture(view, path, w=2000, content_aspect=None):
    """截图比例跟视口一致（ViewCapture 在比例不一致时保留竖向范围、横向外扩，
    正交图会留出大片空白）；给了 content_aspect 就再按内容比例居中裁一刀。"""
    view.Redraw()
    Rhino.RhinoApp.Wait()
    vs = view.ActiveViewport.Size
    aspect = float(vs.Width) / max(1, vs.Height)
    h = int(round(w / aspect))
    vc = Rhino.Display.ViewCapture()
    vc.Width = w
    vc.Height = h
    vc.ScaleScreenItems = False
    vc.DrawAxes = False
    vc.DrawGrid = False
    vc.DrawGridAxes = False
    vc.TransparentBackground = False
    bmp = vc.CaptureToBitmap(view)
    if bmp is None:
        raise RuntimeError("截图失败：" + path)
    if content_aspect:
        cw, ch = w, h
        if content_aspect < aspect:
            cw = min(w, int(h * content_aspect * 1.03))
        else:
            ch = min(h, int(w / content_aspect * 1.03))
        rect = SD.Rectangle((w - cw) // 2, (h - ch) // 2, cw, ch)
        bmp = bmp.Clone(rect, bmp.PixelFormat)
    bmp.Save(path, SD.Imaging.ImageFormat.Png)
    step("出图 %s（%d×%d，视口 %d×%d）" % (os.path.basename(path), bmp.Width, bmp.Height, vs.Width, vs.Height))


TOPS = ("结构", "幕墙", "分析", "标注", "施工总平面", "型录")  # 出图时逐层开关


def only(*visible):
    for path in _layers:
        on = any(path == v or path.startswith(v + "::") or v.startswith(path + "::") for v in visible)
        if path.split("::")[0] in TOPS:
            set_visible(path, on)
    DOC.Views.Redraw()


def shoot(img_dir):
    view = DOC.Views.Find("Perspective", False) or DOC.Views.ActiveView
    DOC.Views.ActiveView = view
    view.Maximized = True
    vp = view.ActiveViewport
    rendered = display_mode("Rendered", "Shaded")
    shaded = display_mode("Shaded", "Rendered")

    def persp(target, cam, lens=35):
        vp.ChangeToPerspectiveProjection(True, lens)
        vp.SetCameraLocations(RG.Point3d(*target), RG.Point3d(*cam))

    def ortho(proj, lo, hi, pad=1.04):
        """正交取景，返回内容的宽高比供截图裁剪用。"""
        vp.SetProjection(proj, None, False)
        dx, dy, dz = hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]
        mx, my, mz = dx * (pad - 1) / 2.0, dy * (pad - 1) / 2.0, dz * (pad - 1) / 2.0
        bb = RG.BoundingBox(RG.Point3d(lo[0] - mx, lo[1] - my, lo[2] - mz), RG.Point3d(hi[0] + mx, hi[1] + my, hi[2] + mz))
        vp.ZoomBoundingBox(bb)
        return (dx * pad) / ((dz if proj == Rhino.Display.DefinedViewportProjection.Front else dy) * pad)

    top = C.LEVELS[-1][1] + C.LEVELS[-1][2]
    only("结构", "幕墙", "施工总平面::场地红线", "施工总平面::场地", "施工总平面::坠物禁区",
         "施工总平面::堆场架位", "施工总平面::车道", "施工总平面::大门")
    vp.DisplayMode = rendered
    persp((OUT_X * 0.55, OUT_Y * 0.45, top * 0.38), (-38000, -62000, 34000))
    capture(view, os.path.join(img_dir, "hero.png"), 2000)

    only("结构", "幕墙", "标注")
    vp.DisplayMode = rendered
    persp((5600, 0, 4300), (-3500, -14500, 4800), lens=35)
    capture(view, os.path.join(img_dir, "detail_sw.png"), 2000)

    only("分析::展开立面")
    vp.DisplayMode = shaded
    x0, z0, x1, z1 = unfolded_bbox()
    ca = ortho(Rhino.Display.DefinedViewportProjection.Front, (x0, UNFOLD_Y, z0), (x1, UNFOLD_Y, z1), 1.02)
    capture(view, os.path.join(img_dir, "unfolded.png"), 2400, ca)

    only("分析::按安装周", "结构", "幕墙::转角立柱")
    vp.DisplayMode = shaded
    persp((OUT_X * 0.5, OUT_Y * 0.5, top * 0.42), (-30000, -52000, 40000))
    capture(view, os.path.join(img_dir, "install_weeks.png"), 2000)

    only("施工总平面", "结构", "幕墙")
    vp.DisplayMode = shaded
    ca = ortho(Rhino.Display.DefinedViewportProjection.Top, (C.SITE[0], C.SITE[1], 0), (C.SITE[2], C.SITE[3], 0), 1.04)
    capture(view, os.path.join(img_dir, "site_plan.png"), 2000, ca)

    only("型录")
    vp.DisplayMode = rendered
    persp((6800, CATALOG_Y + 90, 2100), (-2500, CATALOG_Y - 13500, 4200), lens=40)
    capture(view, os.path.join(img_dir, "panel_types.png"), 2000)
    only(*TOPS)


def main():
    t0 = time.time()
    DOC.ModelUnitSystem = Rhino.UnitSystem.Millimeters
    DOC.ModelAbsoluteTolerance = 0.1
    r = compute()
    step("算完排程：%d 块板，%d 个架子，%d 车" % (len(r["panels"]), len(r["stillages"]), len(r["trucks"])))
    DOC.Strings.SetString("项目", "单元式幕墙 BIM（虚构示例建筑）")
    DOC.Strings.SetString("外轮廓_mm", "%d x %d" % (OUT_X, OUT_Y))
    DOC.Strings.SetString("板块数", str(len(r["panels"])))
    build(r)
    img_dir = os.path.join(ROOT, "docs", "img")
    if not os.path.isdir(img_dir):
        os.makedirs(img_dir)
    shoot(img_dir)
    out = os.path.join(ROOT, "model", "facade_bim.3dm")
    opt = Rhino.FileIO.FileWriteOptions()
    for k, v in (("IncludeRenderMeshes", False), ("IncludeHistory", False), ("IncludePreviewImage", True)):
        try:
            setattr(opt, k, v)            # 不存渲染网格：打开时 Rhino 会重算，文件小一大截
        except Exception:
            pass
    ok = DOC.WriteFile(out, opt)
    step("存盘 facade_bim.3dm：%s" % ok)
    LOG.update({"ok": bool(ok), "seconds": round(time.time() - t0, 1), "objects": DOC.Objects.Count,
                "rhino": str(Rhino.RhinoApp.Version)})


try:
    main()
except Exception:
    LOG["ok"] = False
    LOG["error"] = traceback.format_exc()
finally:
    DOC.Modified = False
    with open(os.path.join(ROOT, "model", "build_log.json"), "w", encoding="utf-8") as f:
        json.dump(LOG, f, ensure_ascii=False, indent=1)

"""板块生成：几何、类型、分区面积、重量，全部由 config 推出。

坐标：mm；x 向东，y 向北，z 向上；原点在外轮廓西南角。
每块板的局部坐标：x 沿板宽（0..w），y 为进深（0 = 外表面，DEPTH = 内表面），
z 向上。四个立面分别绕 z 旋转 0/90/180/270 度放置——Rhino 里用同一套
变换放块实例，IFC 里用同一套变换放构件，两边的位置因此逐毫米一致。
"""
import math
from dataclasses import dataclass, field

from . import config as C

OUT_X = 2 * C.CORNER + C.N_LONG * C.MODULE    # 外轮廓东西向总长
OUT_Y = 2 * C.CORNER + C.N_SHORT * C.MODULE   # 外轮廓南北向总长
ROTATION = {"S": 0, "E": 90, "N": 180, "W": 270}

TYPE_NAMES = {
    "U1": "首层标准单元",
    "U2": "标准层单元",
    "U3": "机房百叶单元",
    "U4": "首层入口门单元",
    "U5": "女儿墙单元",
}


@dataclass
class Panel:
    pid: str
    elev: str
    level: str
    col: int
    ptype: str
    w: int                      # 制作宽（mm）
    h: int                      # 制作高（mm）
    origin: tuple               # 局部原点的世界坐标（mm）
    rot: int                    # 绕 z 旋转角（度）
    zones: dict = field(default_factory=dict)   # 分区高度（mm），自下而上
    qty: dict = field(default_factory=dict)     # 本板工程量
    weight_kg: float = 0.0      # 吊装重（不含支座）


def to_world(origin, rot, lx, ly, lz):
    """局部坐标 → 世界坐标，与 Rhino 的块实例变换一致。"""
    a = math.radians(rot)
    ca, sa = round(math.cos(a)), round(math.sin(a))   # 只用 0/90/180/270，取整避免浮点尾差
    ox, oy, oz = origin
    return (ox + lx * ca - ly * sa, oy + lx * sa + ly * ca, oz + lz)


def panel_type(level, elev, col):
    if level == "RF":
        return "U5"
    if col in C.DOOR_MODULES.get((level, elev), ()):
        return "U4"
    if col in C.LOUVER_MODULES.get((level, elev), ()):
        return "U3"
    return "U1" if level == "L1" else "U2"


def module_origin(elev, col, z):
    """该列板块局部原点（外表面、左下角）的世界坐标。"""
    j = C.JOINT / 2.0
    i = col - 1
    if elev == "S":
        return (C.CORNER + i * C.MODULE + j, 0.0, z + j)
    if elev == "E":
        return (float(OUT_X), C.CORNER + i * C.MODULE + j, z + j)
    if elev == "N":
        return (C.CORNER + C.N_LONG * C.MODULE - i * C.MODULE - j, float(OUT_Y), z + j)
    if elev == "W":
        return (0.0, C.CORNER + C.N_SHORT * C.MODULE - i * C.MODULE - j, z + j)
    raise ValueError(elev)


def zones_for(ptype, h):
    """板块自下而上的分区高度。横框计入分区，面积只算填充区。"""
    t = C.TRANSOM_H
    if ptype == "U5":
        return [("transom", t), ("spandrel", h - 2 * t), ("transom", t)]
    if ptype == "U4":
        vision = h - 4 * t - C.DOOR_LEAF_H - C.SPANDREL_H
        return [("transom", t), ("door", C.DOOR_LEAF_H), ("transom", t),
                ("vision", vision), ("transom", t), ("spandrel", C.SPANDREL_H), ("transom", t)]
    infill = "louver" if ptype == "U3" else "vision"
    return [("transom", t), (infill, h - 3 * t - C.SPANDREL_H), ("transom", t),
            ("spandrel", C.SPANDREL_H), ("transom", t)]


def quantities(ptype, w, h, zones):
    """由分区尺寸和 config 里的面密度 / 线密度算工程量与吊装重。"""
    net_w = w - 2 * C.MULLION_W
    area = {"vision": 0.0, "spandrel": 0.0, "louver": 0.0, "door": 0.0}
    n_transom = 0
    for kind, zh in zones:
        if kind == "transom":
            n_transom += 1
        else:
            area[kind] += net_w * zh / 1e6
    spandrel_kg_m2 = (C.SPANDREL_GLASS_MM * C.GLASS_DENSITY + C.BACKPAN_MM * C.ALU_DENSITY
                      + C.INSULATION_MM * C.MINERAL_WOOL_DENSITY) / 1000.0
    mullion_kg = 2 * h / 1000.0 * C.MULLION_KG_M
    transom_kg = n_transom * net_w / 1000.0 * C.TRANSOM_KG_M
    span = coping_span(w) if ptype == "U5" else (0.0, 0.0)
    coping_kg = C.COPING_W * (span[1] - span[0]) * C.COPING_MM / 1e9 * C.ALU_DENSITY
    q = {
        "vision_igu_m2": area["vision"],
        "spandrel_m2": area["spandrel"],
        "louver_m2": area["louver"],
        "door_glass_m2": area["door"],
        "frame_alu_kg": mullion_kg + transom_kg,
        "backpan_alu_kg": area["spandrel"] * C.BACKPAN_MM / 1000.0 * C.ALU_DENSITY,
        "coping_alu_kg": coping_kg,
        "insulation_m2": area["spandrel"],
        "brackets": C.BRACKETS_PER_PANEL,
    }
    weight = (area["vision"] * C.IGU_GLASS_MM / 1000.0 * C.GLASS_DENSITY
              + area["spandrel"] * spandrel_kg_m2
              + area["louver"] * C.LOUVER_KG_M2
              + area["door"] * C.DOOR_GLASS_MM / 1000.0 * C.GLASS_DENSITY
              + (C.DOOR_HARDWARE_KG if ptype == "U4" else 0.0)
              + mullion_kg + transom_kg + coping_kg)
    return q, weight


def coping_span(w):
    """Actual fabrication span in panel-local X, shared by geometry and take-off.

    End joints are a configurable demonstration assumption. Their adequacy is
    checked independently in the model; this function does not relax clearance.
    """
    values = (w, C.JOINT, C.COPING_END_JOINT, C.COPING_W, C.COPING_MM, C.ALU_DENSITY)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
           not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("压顶尺寸、端缝和材料参数必须为有限正数")
    length = w + C.JOINT - C.COPING_END_JOINT
    if length <= 0:
        raise ValueError("压顶总端缝必须小于板块模数，制造长度必须为正")
    start = (C.COPING_END_JOINT - C.JOINT) / 2.0
    return start, start + length


def build_panels():
    """按安装顺序（逐层；每层南→东→北→西；每面自左向右）生成全部板块。"""
    panels = []
    for level, z, lvl_h in C.LEVELS:
        h = lvl_h - C.JOINT
        w = C.MODULE - C.JOINT
        for elev, ncol in C.ELEVATIONS:
            for col in range(1, ncol + 1):
                ptype = panel_type(level, elev, col)
                zones = zones_for(ptype, h)
                q, wt = quantities(ptype, w, h, zones)
                panels.append(Panel(
                    pid="%s-%s-%02d" % (elev, level, col), elev=elev, level=level, col=col,
                    ptype=ptype, w=w, h=h, origin=module_origin(elev, col, z),
                    rot=ROTATION[elev], zones=dict(zones_to_named(zones)), qty=q,
                    weight_kg=round(wt, 1)))
    return panels


def zones_to_named(zones):
    """把分区列表转成带序号的字典，便于写进 CSV / UserText。"""
    out, seen = [], {}
    for kind, zh in zones:
        seen[kind] = seen.get(kind, 0) + 1
        out.append(("%s%d" % (kind, seen[kind]), zh))
    return out


def corner_posts():
    """四个转角立柱（x0, y0, 边长, 高）。"""
    top = C.LEVELS[-1][1] + C.LEVELS[-1][2]
    s = C.CORNER
    return [(0, 0, s, top), (OUT_X - s, 0, s, top), (OUT_X - s, OUT_Y - s, s, top), (0, OUT_Y - s, s, top)]


def footprint(p):
    """板块的平面包络矩形 (xmin, ymin, xmax, ymax)。"""
    pts = [to_world(p.origin, p.rot, lx, ly, 0) for lx in (0, p.w) for ly in (0, C.DEPTH)]
    xs, ys = [q[0] for q in pts], [q[1] for q in pts]
    return (min(xs), min(ys), max(xs), max(ys))

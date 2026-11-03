# -*- coding: utf-8 -*-
"""蜗轮箱（图号 LJT06.14）参数化重建 —— 单文件版本，可直接跑。

===============================================================================
一、坐标与主要尺寸
===============================================================================
坐标约定（与实物照片一致）：
    +Y = 大孔（蜗轮腔）轴线，朝向观察者
    -X = 蜗杆轴线，蜗杆凸台向这一侧伸出
    +Z = 上方

主要尺寸见 PARAMS（★ = 图纸/照片已确认，估 = 仍需图纸确认）：
    主体 Ø64 × 52、内腔 Ø54（留后壁 4）、蜗轮轴孔 Ø30
    环形凸台 Ø91 × 12、环上 2×Ø8.3（孔心 R36，角度 55° 为估）
    蜗杆凸台 Ø27.5 × 15、Ø15H8 通孔 + Ø24 沉孔、M27×2 细牙
    对侧凸台 R16 / Ø18H8、底部 2 片耳片 × 6 厚、环根圆角 R12.5

===============================================================================
二、运行方式
===============================================================================
    run_journal wormbox_clean.py -args x clean_res.json
    run_journal wormbox_clean.py -args job.json clean_res.json
    run_journal wormbox_clean.py -args --check          # 只校验参数，不建模

job.json 支持的键：
    {"params": {...}, "out_prt": "D:/.../x.prt", "resp": "D:/.../res.json"}
（旧版的 out_step 从未真正导出过 STEP，本版删除；传了会给出警告。）

本文件也可以直接用 python 运行 `--check`（不需要 NX），便于在 CI/本地
校验参数，例如：
    python wormbox_clean.py --check

===============================================================================
三、NX 硬约束（都是实测校准出来的，改代码时不要改回去）
===============================================================================
1) 拉伸的 StartExtend / EndExtend 是**相对草图平面的距离**（mm），
   不是世界绝对坐标。证据：对侧凸台草图在 x=30、end=22，最终 bbox
   最大 X=52；若按绝对坐标解释应为 22。旧注释里"绝对坐标"的说法有误。
   （20³ 方块 start=2/end=6 切掉 4mm 的实验只能证明"跨度=4mm"，无法
   区分绝对坐标与相对距离，因此旧结论不能作为判据。）

2) 需要并集时优先 `mode="create"` 建独立体，最后用 unite_all() 一次合并。
   实测日志里 02/06/08 的拉伸内置 Unite 也生效（体积增加且体数为 1），
   但独立实体统一在 10_unite 合并更可控，不依赖该行为。

3) collector 布尔必须设 KeepRemove=Remove，否则原体保留、等于没并：
       region.KeepRemoveTargetMethod = ...
       region.KeepRemoveToolMethod   = ...

4) 并集后 NX 会换新体，旧的 body 引用失效 -> 每轮重新取主体
   （本文件用 main_body()：面数最多的实体）。

5) edge.GetVertices() 返回 **Point3d**（.X/.Y/.Z），没有 .value。
   按 .value 读永远得到空列表 —— 圆角"选不到边"的根因。

6) 布尔要求**体积重叠**，共面不算相交：
       - 工具完全在目标外 -> "Tool body completely outside target body"
       - 工具末端停在离壁 1 mm -> "zero wall thickness"
   -> 拉伸范围要么穿进去，要么伸出来，别停在面上或壁前。

7) 螺纹：M27×2 是**细牙**，ThreadStandard 要 "Metric Fine"；
   CylindricalFace 是对象，必须 builder.CylindricalFace.Value = face。

8) 圆角不能一次选一圈**不同半径**的相邻相切边：
   "Adjacent tangent edges blended at the same time must have the same blend"
   -> 本版按"共享端点"把候选边分成链，同一条链用同一个半径一起加；
      链失败时退化为逐边 try/except。

===============================================================================
四、本版相对上一版的主要改动
===============================================================================
* 参数与几何继续分离；新增 --check 参数校验（不需要启动 NX）。
* 输出目录/响应文件的创建逻辑修正（旧版对 OUT_STEP 也用 OUT_PRT 的目录，
  且 RESP 目录未创建）。
* 蜗杆凸台改为独立实体 -> 先在完整圆柱上加 M27×2 符号螺纹 -> 最后并集
  （对应交接文档 §5 的建议；失败只记日志，不中断建模）。
* 圆角改为"确定性排序 + 相切边链分组"，链失败退化为逐边，降低
  每次运行结果不同的问题；未注圆角 R2.5 仍默认关闭（R_FILLET_SMALL=0）。
* 步骤编号与执行顺序一致；逐步断言体数/记录体积/面数保留。
* 错误记录更简洁（异常最后一行 + 步名），LOG["ok"] 仍然汇总所有步骤。

已知未完成（需要图纸/实物确认，本版不猜）：
    - 支臂（30° 左偏、R9 端、Ø10.5 孔、孔心距 44.25）：缺基准/图纸，未建模。
    - 底部耳片在 Y 向的位置、环上两孔角度：仍为估值，见 PARAMS 注释。
"""
import json
import math
import os
import re
import sys
import traceback

try:  # 允许在没有 NX 的机器上跑 --check / 单元测试
    import NXOpen
except ImportError:  # pragma: no cover - 只在 NX 之外发生
    NXOpen = None

if NXOpen is not None:
    for _name in ("Features", "Sections", "GeometricUtilities", "SmartObject",
                  "Curves", "Points"):
        try:
            __import__("NXOpen." + _name)
        except Exception:
            pass


# ============================================================================
# 参数（全部可改；★ 是图纸/照片确认值，估 = 仍待确认）
# ============================================================================
DEFAULT_PARAMS = {
    # ---- 主体与腔 ----
    "D_BODY": 64.0,          # 主体外径 ★
    "L_BODY": 52.0,          # 主体长度（26+26）★
    "D_CAVITY": 54.0,        # 内腔直径 ★
    "T_BACK": 4.0,           # 后壁厚（= 52 - 腔深）★
    "D_BORE": 30.0,          # 蜗轮轴孔 ★
    # ---- 环形凸台 ----
    "D_RING": 91.0,          # 环形凸台外径 ★
    "T_RING": 12.0,          # 环厚 ★
    "N_RING_HOLES": 2,       # 环上孔数 ★（本实现只支持 2 个对称孔）
    "D_RING_HOLE": 8.3,      # 环上孔径 ★
    "R_RING_HOLE": 36.0,     # 孔心半径（估）
    "RING_HOLE_ANGLE": 55.0,  # 孔相对 +Z 的角度（估）
    # ---- 蜗杆凸台 ----
    "D_WORM": 27.5,          # 凸台外径 ★（= 螺纹 M27 前的外圆）
    "L_WORM": 15.0,          # 伸出长度 ★
    "D_WORM_BORE": 15.0,     # Ø15H8 ★
    "D_CBORE": 24.0,         # 沉孔 ★
    "L_CBORE": 10.0,         # 沉孔深（估）
    "THREAD": "M27 x 2",     # 细牙 ★
    "THREAD_ENABLED": True,  # 符号螺纹开关（并集异常时可先关掉定位）
    # ---- 对侧凸台 ----
    "D_SIDE": 18.0,          # Ø18H8 ★
    "R_SIDE": 16.0,          # 外圆 R16 ★
    "L_SIDE": 22.0,          # 伸出 22（估）
    # ---- 底部耳片 ----
    "LUG_T": 6.0,            # 厚度 ★
    "LUG_GAP": 26.0,         # 两片间距 ★
    "LUG_W": 44.0,           # 宽度（X 向，估）
    "LUG_H": 6.0,            # 伸出高度（估）
    "LUG_DROP": 6.0,         # 再往主体里多伸 6mm，保证与主体相交（估）
    "LUG_Y0": 2.0,           # 第一片在 Y 向的起点（估）
    "D_LUG_HOLE": 8.3,       # 孔径 ★
    # ---- 圆角 ----
    "R_FILLET_RING": 12.5,   # 环根 ★
    "R_FILLET_SMALL": 0.0,   # 未注圆角；0 = 关闭（见文件头"已知未完成"）
}

PARAMS = dict(DEFAULT_PARAMS)

# 运行输出默认值（保持与旧版一致；可用 job.json 覆盖）
OUT_PRT = r"E:\deepseekharness\nx-workspace\explode\worm\wormbox_clean.prt"
RESP = r"E:\deepseekharness\nx-workspace\explode\worm\clean_res.json"


# ============================================================================
# 运行状态
# ============================================================================
SESSION = None
PART = {"part": None}
LOG = {"params": {}, "steps": []}
_UNITS = []

# 由 --check 识别出的非建模参数
_FLAGS = {"--check"}


# ============================================================================
# 记录与测量
# ============================================================================
def _units(part):
    """按需缓存单位集合，避免每步重复查找。"""
    if not _UNITS:
        _UNITS.extend(part.UnitCollection.FindObject(name) for name in
                      ("SquareMilliMeter", "CubicMilliMeter", "Kilogram",
                       "MilliMeter", "Newton"))
    return _UNITS


def _measure(part):
    """(体数, 总体积, 总面数)。多体时体积是各体之和，重叠会重复计算。"""
    units = _units(part)
    volume, faces, count = 0.0, 0, 0
    for body in part.Bodies:
        count += 1
        faces += len(list(body.GetFaces()))
        try:
            volume += float(
                part.MeasureManager.NewMassProperties(units, 0.99, [body]).Volume)
        except Exception:
            pass
    return count, round(volume, 2), faces


def _exc_tail(exc):
    """异常的最后一行有效信息（NX 异常常是多行字符串）。"""
    text = str(exc).strip() or exc.__class__.__name__
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (lines[-1] if lines else exc.__class__.__name__)[:240]


def step(name, fn, expect_bodies=1):
    """跑一步，记录体积/面数，并断言体数。返回是否成功。"""
    record = {"name": name}
    try:
        record["result"] = fn()
        record["ok"] = True
    except Exception as exc:
        record["ok"] = False
        record["error"] = [line for line in traceback.format_exc().splitlines()
                           if line.strip()][-4:]
        record["error"].append("last: " + _exc_tail(exc))

    try:
        count, volume, faces = _measure(PART["part"])
        record.update({"bodies": count, "volume": volume, "faces": faces})
        if expect_bodies is not None and count != expect_bodies:
            record["ok"] = False
            record.setdefault("error", []).append(
                "expected %d bodies, got %d" % (expect_bodies, count))
    except Exception:
        pass

    LOG["steps"].append(record)
    return record.get("ok", False)


def _write_json(path, data):
    """写 JSON，并确保父目录存在。"""
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)


# ============================================================================
# 几何辅助（所有"坑"都封装在这里）
# ============================================================================
def circle_xz(cx, cz, r, y=0.0):
    """XZ 平面圆（法向 Y）—— 大孔方向用它"""
    return PART["part"].Curves.CreateArc(
        NXOpen.Point3d(float(cx), float(y), float(cz)),
        NXOpen.Vector3d(1.0, 0.0, 0.0), NXOpen.Vector3d(0.0, 0.0, 1.0),
        float(r), 0.0, 2.0 * math.pi)


def circle_yz(cy, cz, r, x=0.0):
    """YZ 平面圆（法向 X）—— 蜗杆方向用它"""
    return PART["part"].Curves.CreateArc(
        NXOpen.Point3d(float(x), float(cy), float(cz)),
        NXOpen.Vector3d(0.0, 1.0, 0.0), NXOpen.Vector3d(0.0, 0.0, 1.0),
        float(r), 0.0, 2.0 * math.pi)


def polyline_xz(points, y=0.0):
    """XZ 平面上的闭合折线"""
    curves = []
    part = PART["part"]
    for index, point in enumerate(points):
        nxt = points[(index + 1) % len(points)]
        curves.append(part.Curves.CreateLine(
            NXOpen.Point3d(float(point[0]), float(y), float(point[1])),
            NXOpen.Point3d(float(nxt[0]), float(y), float(nxt[1]))))
    return curves


def extrude(curves, start, end, mode, direction, target=None):
    """拉伸。start/end 是**相对草图平面**的起点/终点距离（mm）。

    mode: "create" | "unite" | "subtract"
    独立实体请用 "create" 并在最后 unite_all()；详见文件头约束 2。
    """
    part = PART["part"]
    sections = part.Sections.CreateSectionsUsingCurves(
        curves, NXOpen.SectionCollection.LoopOption.SeparateOuter, 0.01, 0.01, 0.5)
    section = list(sections[0]) if isinstance(sections, tuple) else (
        sections if isinstance(sections, list) else [sections])

    builder = part.Features.CreateExtrudeBuilder(NXOpen.Features.Feature.Null)
    builder.Section = section[0]
    builder.Direction = part.Directions.CreateDirection(
        NXOpen.Point3d(0.0, 0.0, 0.0),
        NXOpen.Vector3d(float(direction[0]), float(direction[1]), float(direction[2])),
        NXOpen.SmartObject.UpdateOption.WithinModeling)
    builder.Limits.StartExtend.SetValue("%.4f" % float(start))
    builder.Limits.EndExtend.SetValue("%.4f" % float(end))
    if mode != "create":
        enum = NXOpen.GeometricUtilities.BooleanOperation.BooleanType
        body = target if target is not None else main_body()
        builder.BooleanOperation.SetBooleanOperationAndBody(
            {"unite": enum.Unite, "subtract": enum.Subtract}[mode], body)
    feature = builder.Commit()
    builder.Destroy()
    bodies = list(feature.GetBodies()) if hasattr(feature, "GetBodies") else []
    return feature, (bodies[0] if bodies else None)


def extrude_solid(curves, start, end, direction):
    """建独立实体（语义化入口，避免 mode 字符串写错）。"""
    return extrude(curves, start, end, "create", direction)


def extrude_cut(curves, start, end, direction, target=None):
    """从目标体上切除（语义化入口）。"""
    return extrude(curves, start, end, "subtract", direction, target)


def extrude_unite(curves, start, end, direction, target=None):
    """拉伸并并入目标体（语义化入口）。"""
    return extrude(curves, start, end, "unite", direction, target)


def main_body():
    """主体 = 面数最多的实体（并集后 NX 会换新体，不能缓存引用）。"""
    solids = [body for body in PART["part"].Bodies
              if not getattr(body, "IsSheetBody", False)]
    return max(solids, key=lambda body: len(list(body.GetFaces()))) if solids else None


def _body_collector(bodies):
    part = PART["part"]
    collector = part.ScCollectors.CreateCollector()
    options = part.ScRuleFactory.CreateRuleOptions()
    options.SetSelectedFromInactive(False)
    collector.ReplaceRules(
        [part.ScRuleFactory.CreateRuleBodyDumb(list(bodies), True, options)], False)
    options.Dispose()
    return collector


def unite_all():
    """把所有散落的实体并进主体（collector 布尔 + KeepRemove=Remove）。

    每轮重新取主体；一次只并一个工具（一次传多个时第二个会失败）。
    """
    part = PART["part"]
    enum = NXOpen.Features.Feature.BooleanType
    merged = 0
    for _ in range(12):
        main = main_body()
        if main is None:
            break
        tools = [body for body in part.Bodies
                 if int(body.Tag) != int(main.Tag)
                 and not getattr(body, "IsSheetBody", False)]
        if not tools:
            break
        builder = part.Features.CreateBooleanBuilderUsingCollector(
            NXOpen.Features.BooleanFeature.Null)
        try:
            builder.Tolerance = 0.001
            builder.Operation = enum.Unite
            region = builder.BooleanRegionSelect
            option = NXOpen.GeometricUtilities.BooleanRegionSelect.KeepRemoveOption.Remove
            region.KeepRemoveTargetMethod = option
            region.KeepRemoveToolMethod = option
            builder.TargetBodyCollector = _body_collector([main])
            builder.ToolBodyCollector = _body_collector([tools[0]])
            region.AssignTargets([main])
            builder.Commit()
            merged += 1
        finally:
            builder.Destroy()
    return {"merged": merged, "bodies": len(list(part.Bodies))}


# ============================================================================
# 圆角辅助
# ============================================================================
def _vertex_key(vertex, digits=3):
    return (round(vertex.X, digits), round(vertex.Y, digits), round(vertex.Z, digits))


def _edge_key(edge):
    """边的稳定排序键（端点坐标），用于消除每次运行的顺序差异。"""
    try:
        points = sorted(_vertex_key(vertex) for vertex in edge.GetVertices())
    except Exception:
        points = []
    return tuple(points)


def _edge_endpoints(edge):
    try:
        return [_vertex_key(vertex) for vertex in edge.GetVertices()]
    except Exception:
        return []


def edge_radii(edge):
    """边的两个端点到 Z 轴（XZ 面内）的半径；拿不到返回 None。"""
    try:
        vertices = list(edge.GetVertices())
        if len(vertices) != 2:
            return None
        return (math.hypot(vertices[0].X, vertices[0].Z),
                math.hypot(vertices[1].X, vertices[1].Z))
    except Exception:
        return None


def _edge_mid_radius(edge):
    """边的参数中点半径；API 不可用或失败时返回 None（不过滤）。"""
    try:
        point = edge.Evaluate(0.5)
        return math.hypot(point.X, point.Z)
    except Exception:
        return None


def circular_edges_at(radius, tol=0.6):
    """挑"到 Y 轴半径 ≈ radius 且两端相等"的圆边，并按几何键排序。

    中点半径检查可排除两端恰好同半径的直线/接缝边。
    """
    picked = []
    body = main_body()
    if body is None:
        return picked
    for edge in body.GetEdges():
        radii = edge_radii(edge)
        if not radii:
            continue
        if abs(radii[0] - radii[1]) >= 0.3:
            continue
        if abs(radii[0] - radius) >= tol:
            continue
        mid = _edge_mid_radius(edge)
        if mid is not None and abs(mid - radius) >= tol:
            continue
        picked.append(edge)
    picked.sort(key=_edge_key)
    return picked


def group_tangent_edges(edges):
    """按共享端点把候选边连成链，返回 [[edge, ...], ...]（顺序确定）。"""
    items = sorted(edges, key=_edge_key)
    parent = list(range(len(items)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[max(root_left, root_right)] = min(root_left, root_right)

    owner = {}
    for index, edge in enumerate(items):
        for key in _edge_endpoints(edge):
            if key in owner:
                union(index, owner[key])
            else:
                owner[key] = index

    groups = {}
    for index, edge in enumerate(items):
        groups.setdefault(find(index), []).append(edge)
    return [groups[key] for key in sorted(groups)]


def _blend(edges, radius):
    """把一组边用同一半径一次加圆角；成功返回 True。"""
    part = PART["part"]
    builder = part.Features.CreateEdgeBlendBuilder(NXOpen.Features.Feature.Null)
    try:
        collector = part.ScCollectors.CreateCollector()
        collector.ReplaceRules(
            [part.ScRuleFactory.CreateRuleEdgeDumb(list(edges))], False)
        builder.Tolerance = 0.001
        builder.RollOntoEdge = True
        builder.RollOverSmoothEdge = True
        builder.MoveSharpEdge = True
        builder.AddChainset(collector, "%.3f" % radius)
        builder.Commit()
        return True
    except Exception:
        return False
    finally:
        builder.Destroy()


def fillet_chains(edges, radius):
    """先整条链一次加，失败再逐边加；返回统计。

    相邻相切边必须用相同 blend，所以按链分组、同半径一起提交。
    """
    groups = group_tangent_edges(edges)
    stats = {"edges": len(edges), "chains": len(groups),
             "chains_ok": 0, "edges_ok": 0}
    for group in groups:
        if _blend(group, radius):
            stats["chains_ok"] += 1
            stats["edges_ok"] += len(group)
            continue
        for edge in group:
            if _blend([edge], radius):
                stats["edges_ok"] += 1
    return stats


# ============================================================================
# 建模步骤
# ============================================================================
def build():
    part = PART["part"]
    P = PARAMS

    # ---- 1 主体：Ø64 沿 +Y，0..L_BODY ----
    def s_body():
        extrude_solid([circle_xz(0.0, 0.0, P["D_BODY"] / 2.0)],
                      0.0, P["L_BODY"], (0.0, 1.0, 0.0))
        return {"bodies": len(list(part.Bodies))}
    step("01_body", s_body)

    # ---- 2 环形凸台 Ø91（与大孔同轴，在 +Y 前端面） ----
    def s_ring():
        extrude_unite([circle_xz(0.0, 0.0, P["D_RING"] / 2.0)],
                      -P["T_RING"], 0.0, (0.0, 1.0, 0.0))
        return {"ring": P["D_RING"]}
    step("02_ring", s_ring)

    # ---- 3 内腔 Ø54（留后壁 T_BACK） ----
    def s_cavity():
        extrude_cut([circle_xz(0.0, 0.0, P["D_CAVITY"] / 2.0)],
                    -P["T_RING"], P["L_BODY"] - P["T_BACK"], (0.0, 1.0, 0.0))
        return {"cavity": P["D_CAVITY"]}
    step("03_cavity", s_cavity)

    # ---- 4 蜗轮轴孔 Ø30 ----
    def s_bore():
        extrude_cut([circle_xz(0.0, 0.0, P["D_BORE"] / 2.0)],
                    -P["T_RING"] - 1.0, P["L_BODY"] + 1.0, (0.0, 1.0, 0.0))
        return {"bore": P["D_BORE"]}
    step("04_bore", s_bore)

    # ---- 5 环上 2×Ø8.3 ----
    def s_ring_holes():
        angle = math.radians(P["RING_HOLE_ANGLE"])
        radius = P["R_RING_HOLE"]
        for sign in (1.0, -1.0):
            cx = sign * radius * math.sin(angle)
            cz = radius * math.cos(angle)
            extrude_cut([circle_xz(cx, cz, P["D_RING_HOLE"] / 2.0)],
                        -P["T_RING"] - 1.0, P["T_RING"] + 1.0, (0.0, 1.0, 0.0))
        return {"ring_holes": P["N_RING_HOLES"]}
    step("05_ring_holes", s_ring_holes)

    # ---- 6 蜗杆凸台 Ø27.5：先建独立完整圆柱，便于加螺纹 ----
    def s_worm_boss():
        r_body = P["D_BODY"] / 2.0
        _feature, body = extrude_solid(
            [circle_yz(0.0, 0.0, P["D_WORM"] / 2.0, -r_body + 2.0)],
            0.0, P["L_WORM"], (-1.0, 0.0, 0.0))
        return {"worm": P["D_WORM"], "len": P["L_WORM"],
                "body": None if body is None else str(body.Tag)}
    step("06_worm_boss", s_worm_boss, expect_bodies=None)  # 独立体，待并

    # ---- 7 蜗杆凸台符号螺纹（在完整圆柱上做，再并） ----
    def s_thread():
        if not P["THREAD_ENABLED"]:
            return "skip (THREAD_ENABLED=False)"
        faces = _worm_boss_cylinders()
        if not faces:
            return "skip (no worm boss cylinder)"
        thread = str(P["THREAD"])
        combos = []
        for standard in ("Metric Fine", "Metric Coarse"):
            for size in (thread, thread.replace(" ", "")):
                if (standard, size) not in combos:
                    combos.append((standard, size))
        # 兜底：图纸确认值 M27×2 细牙 / M27×3 粗牙
        for pair in (("Metric Fine", "M27 x 2"), ("Metric Coarse", "M27 x 3"),
                     ("Metric Coarse", "M27 x 2")):
            if pair not in combos:
                combos.append(pair)
        errors = []
        for face in faces:
            for standard, size in combos:
                builder = part.Features.CreateThreadBuilder(NXOpen.Features.Thread.Null)
                try:
                    builder.ThreadStandard = standard
                    builder.ThreadMethod = "Cut"
                    builder.CylindricalFace.Value = face
                    builder.ThreadSize = size
                    builder.RadialEngage = "0.75"
                    builder.ThreadLimit = NXOpen.Features.ThreadBuilder.LimitOption.Full
                    builder.ReverseThreadDirection = True
                    feature = builder.Commit()
                    return {"thread": size, "standard": standard,
                            "feature": str(feature.Name)}
                except Exception as exc:
                    errors.append("%s/%s: %s" % (standard, size, _exc_tail(exc)))
                finally:
                    builder.Destroy()
        return {"thread": "skip", "faces": len(faces), "errors": errors[:4]}
    step("07_thread", s_thread, expect_bodies=None)

    # ---- 8 对侧凸台 R16 / Ø18H8（沿 +X） ----
    def s_side_boss():
        r_body = P["D_BODY"] / 2.0
        extrude_unite([circle_yz(0.0, 0.0, P["R_SIDE"], r_body - 2.0)],
                      0.0, P["L_SIDE"], (1.0, 0.0, 0.0))
        extrude_cut([circle_yz(0.0, 0.0, P["D_SIDE"] / 2.0, r_body - 3.0)],
                    -1.0, P["L_SIDE"] + 4.0, (1.0, 0.0, 0.0))
        return {"side": P["D_SIDE"]}
    step("08_side_boss", s_side_boss, expect_bodies=None)

    # ---- 9 底部耳片（2 片，各 Ø8.3）：先建独立体，再并 ----
    def s_lugs():
        z_top = -(P["D_CAVITY"] / 2.0) - 0.5   # 必须在腔底以下，否则插进腔里
        z_bot = z_top - P["LUG_H"] - P["LUG_DROP"]
        y_start = P["LUG_Y0"]
        spans = [(y_start, y_start + P["LUG_T"]),
                 (y_start + P["LUG_GAP"], y_start + P["LUG_GAP"] + P["LUG_T"])]
        for y0, y1 in spans:
            extrude_solid(polyline_xz([(-P["LUG_W"] / 2.0, z_bot),
                                       (P["LUG_W"] / 2.0, z_bot),
                                       (P["LUG_W"] / 2.0, z_top),
                                       (-P["LUG_W"] / 2.0, z_top)]),
                          y0, y1, (0.0, 1.0, 0.0))
            cz = (z_top + z_bot) / 2.0
            extrude_cut([circle_xz(0.0, cz, P["D_LUG_HOLE"] / 2.0)],
                        y0 - 1.0, y1 + 1.0, (0.0, 1.0, 0.0),
                        target=list(part.Bodies)[-1])
        return {"lugs": len(spans)}
    step("09_lugs", s_lugs, expect_bodies=None)   # 此时故意是多体

    # ---- 10 一次并成单实体 ----
    step("10_unite", unite_all)

    # ---- 11 蜗杆孔 Ø15H8 + Ø24 沉孔（并集后从凸台端面往 +X 切） ----
    def s_worm_bore():
        r_body = P["D_BODY"] / 2.0
        x_face = -r_body - P["L_WORM"]          # 名义端面（实际端面在 +2mm）
        # 通孔：起点在端面外 1mm，末端穿进腔体（不能停在离壁 1mm 处）
        extrude_cut([circle_yz(0.0, 0.0, P["D_WORM_BORE"] / 2.0, x_face - 1.0)],
                    -1.0, P["L_WORM"] + 9.0, (1.0, 0.0, 0.0))
        # 沉孔：起点往材料内 3mm（压在端面上会报零壁厚）
        extrude_cut([circle_yz(0.0, 0.0, P["D_CBORE"] / 2.0, x_face + 3.0)],
                    0.0, P["L_CBORE"], (1.0, 0.0, 0.0))
        return {"bore": P["D_WORM_BORE"], "cbore": P["D_CBORE"]}
    step("11_worm_bore", s_worm_bore)

    # ---- 12 环根圆角 R12.5（按相切边链分组，失败退化为逐边） ----
    def s_fillet_ring():
        picked = circular_edges_at(P["D_RING"] / 2.0)
        if not picked:
            return "skip (no ring edge)"
        return fillet_chains(picked, P["R_FILLET_RING"])
    step("12_fillet_ring", s_fillet_ring)

    # ---- 13 未注圆角（默认关闭，见文件头"已知未完成"） ----
    def s_fillet_small():
        radius = P["R_FILLET_SMALL"]
        if radius <= 0:
            return "skip (R_FILLET_SMALL=0)"
        picked = circular_edges_at(P["D_BODY"] / 2.0)
        if not picked:
            return "skip (no body edge)"
        return fillet_chains(picked, radius)
    step("13_fillet_small", s_fillet_small)

    # ---- 14 复核 ----
    def s_verify():
        body = main_body()
        count, volume, faces = _measure(part)
        lo, hi = [1e18] * 3, [-1e18] * 3
        for edge in body.GetEdges():
            for vertex in edge.GetVertices():
                for index, coord in enumerate((vertex.X, vertex.Y, vertex.Z)):
                    lo[index] = min(lo[index], coord)
                    hi[index] = max(hi[index], coord)
        return {"bodies": count, "volume": volume, "faces": faces,
                "bbox": [[round(value, 2) for value in lo],
                         [round(value, 2) for value in hi]]}
    step("14_verify", s_verify)

    def s_save():
        part.Save(NXOpen.BasePart.SaveComponents.TrueValue,
                  NXOpen.BasePart.CloseAfterSave.FalseValue)
        return {"prt": OUT_PRT, "features": len(list(part.Features))}
    step("15_save", s_save)


def _worm_boss_cylinders():
    """凸台外圆的候选圆柱面（先独立体的完整圆柱，后并集后的裁切面）。"""
    P = PARAMS
    target_radius = P["D_WORM"] / 2.0
    # 凸台内端在 x = -(D_BODY/2 - 2)；阈值放宽到 -28，避免永远选不到
    x_limit = -(P["D_BODY"] / 2.0 - 4.0)
    picked = []
    for body in PART["part"].Bodies:
        if getattr(body, "IsSheetBody", False):
            continue
        for face in body.GetFaces():
            try:
                if int(str(getattr(face, "SolidFaceType", None))) != 2:
                    continue
            except Exception:
                continue
            radius_max, x_max = 0.0, -1e18
            for edge in face.GetEdges():
                for vertex in edge.GetVertices():
                    try:
                        radius_max = max(radius_max, math.hypot(vertex.Y, vertex.Z))
                        x_max = max(x_max, vertex.X)
                    except Exception:
                        pass
            if x_max < x_limit and abs(radius_max - target_radius) < 1.2:
                picked.append((radius_max, x_max, face))
    picked.sort(key=lambda item: (-item[0], item[1]))
    return [face for _radius, _x, face in picked]


# ============================================================================
# 参数校验（不依赖 NX，可单独跑）
# ============================================================================
_TAG_RE = re.compile(r"^\s*M\s*(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*$")


def _number(params, key, errors):
    try:
        value = float(params[key])
    except (KeyError, TypeError, ValueError):
        errors.append("%s: 不是有效数字 (%r)" % (key, params.get(key)))
        return None
    if not math.isfinite(value):
        errors.append("%s: 不是有限数字 (%r)" % (key, params[key]))
        return None
    return value


def validate_params(params):
    """返回 (errors, warnings)。不会修改 PARAMS。"""
    errors, warnings = [], []

    for key in sorted(params):
        if key not in DEFAULT_PARAMS:
            warnings.append("未知参数（已忽略）: %s" % key)
    missing = [key for key in DEFAULT_PARAMS if key not in params]
    for key in missing:
        errors.append("缺少参数: %s" % key)
    if missing:
        return errors, warnings

    if not isinstance(params.get("THREAD_ENABLED"), bool):
        errors.append("THREAD_ENABLED 必须是布尔值")

    value = {key: _number(params, key, errors) for key in
             ("D_BODY", "L_BODY", "D_CAVITY", "T_BACK", "D_BORE",
              "D_RING", "T_RING", "D_RING_HOLE", "R_RING_HOLE",
              "RING_HOLE_ANGLE", "D_WORM", "L_WORM", "D_WORM_BORE",
              "D_CBORE", "L_CBORE", "D_SIDE", "R_SIDE", "L_SIDE",
              "LUG_T", "LUG_GAP", "LUG_W", "LUG_H", "LUG_DROP", "LUG_Y0",
              "D_LUG_HOLE", "R_FILLET_RING", "R_FILLET_SMALL")}
    if any(item is None for item in value.values()):
        return errors, warnings

    d_body, l_body = value["D_BODY"], value["L_BODY"]
    d_cavity, t_back, d_bore = value["D_CAVITY"], value["T_BACK"], value["D_BORE"]
    d_ring, t_ring = value["D_RING"], value["T_RING"]
    d_ring_hole, r_ring_hole = value["D_RING_HOLE"], value["R_RING_HOLE"]
    d_worm, l_worm = value["D_WORM"], value["L_WORM"]
    d_worm_bore, d_cbore = value["D_WORM_BORE"], value["D_CBORE"]
    d_side, r_side = value["D_SIDE"], value["R_SIDE"]
    lug_t, lug_gap = value["LUG_T"], value["LUG_GAP"]
    lug_y0, lug_w, lug_h = value["LUG_Y0"], value["LUG_W"], value["LUG_H"]
    d_lug_hole = value["D_LUG_HOLE"]

    if d_body <= 0 or l_body <= 0:
        errors.append("D_BODY / L_BODY 必须为正")
    if not 0 < d_cavity < d_body:
        errors.append("D_CAVITY 必须小于 D_BODY 且为正")
    elif (d_body - d_cavity) / 2.0 < 2.0:
        warnings.append("侧壁厚 < 2mm，布尔运算可能失败")
    if not 0 < d_bore < d_cavity:
        errors.append("D_BORE 必须小于 D_CAVITY（否则轴孔切不穿后壁）")
    if not 0 < t_back < l_body:
        errors.append("T_BACK 必须在 (0, L_BODY) 内")
    elif t_back < 1.0:
        warnings.append("T_BACK < 1mm，后壁过薄")

    if d_ring <= d_body:
        errors.append("D_RING 必须大于 D_BODY（环形凸台要露出来）")
    if t_ring <= 0:
        errors.append("T_RING 必须为正")
    try:
        ring_holes = int(float(params["N_RING_HOLES"]))
    except (TypeError, ValueError):
        errors.append("N_RING_HOLES 必须是数字 2")
    else:
        if ring_holes != 2:
            errors.append("N_RING_HOLES 目前只支持 2（对称两孔）")
    if d_ring_hole <= 0 or r_ring_hole <= 0:
        errors.append("D_RING_HOLE / R_RING_HOLE 必须为正")
    else:
        hole_lo = r_ring_hole - d_ring_hole / 2.0
        hole_hi = r_ring_hole + d_ring_hole / 2.0
        if hole_lo <= d_cavity / 2.0:
            errors.append("环上孔会切进内腔：R_RING_HOLE - D_RING_HOLE/2 <= D_CAVITY/2")
        if hole_hi >= d_ring / 2.0:
            errors.append("环上孔超出凸台外缘：R_RING_HOLE + D_RING_HOLE/2 >= D_RING/2")

    if d_worm <= 0 or l_worm <= 0:
        errors.append("D_WORM / L_WORM 必须为正")
    if not 0 < d_worm_bore < d_worm:
        errors.append("D_WORM_BORE 必须在 (0, D_WORM) 内")
    if not d_worm_bore < d_cbore < d_worm:
        errors.append("D_CBORE 必须大于 D_WORM_BORE 且小于 D_WORM")
    if not 0 < value["L_CBORE"] < l_worm:
        errors.append("L_CBORE 必须在 (0, L_WORM) 内")
    elif value["L_CBORE"] + 3.0 >= l_worm:
        warnings.append("沉孔几乎打穿凸台（L_CBORE + 3 >= L_WORM）")
    match = _TAG_RE.match(str(params.get("THREAD", "")))
    if not match:
        errors.append("THREAD 格式应为 'M27 x 2'（细牙）")
    else:
        thread_major = float(match.group(1))
        if abs(thread_major - d_worm) > 1.5:
            warnings.append("THREAD 公称直径与 D_WORM 相差超过 1.5mm")

    if d_side <= 0 or value["L_SIDE"] <= 0:
        errors.append("D_SIDE / L_SIDE 必须为正")
    if r_side <= d_side / 2.0:
        warnings.append("R_SIDE 不大于 D_SIDE/2，凸台可能包不住孔")

    if lug_t <= 0 or lug_gap <= 0 or lug_w <= 0 or lug_h <= 0:
        errors.append("耳片尺寸必须为正")
    if value["LUG_DROP"] < 0:
        errors.append("LUG_DROP 不能为负")
    if lug_gap <= lug_t:
        errors.append("LUG_GAP 必须大于 LUG_T（两片不能粘连）")
    if lug_y0 < 0:
        errors.append("LUG_Y0 不能为负")
    if lug_y0 + lug_gap + lug_t > l_body:
        errors.append("两片耳片超出主体 Y 向范围（LUG_Y0+LUG_GAP+LUG_T > L_BODY）")
    if d_lug_hole <= 0 or lug_w < d_lug_hole or lug_h + value["LUG_DROP"] < d_lug_hole:
        errors.append("耳片宽度/高度必须能容纳 Ø%.1f 孔" % d_lug_hole)

    if value["R_FILLET_RING"] <= 0:
        errors.append("R_FILLET_RING 必须为正")
    if value["R_FILLET_SMALL"] < 0:
        errors.append("R_FILLET_SMALL 不能为负（0 = 关闭）")

    return errors, warnings


# ============================================================================
# 命令行 / job 解析
# ============================================================================
def _parse_cli(args):
    """解析 run_journal 位置参数（不含脚本名）。

    旧用法是 `-args <job.json|x> <res.json>`：第一个位置参数只有在
    "以 .json 结尾且存在"时才是 job，否则视为占位符；第二个位置参数
    是响应文件。`--check` 可出现在任意位置。
    """
    check = "--check" in args
    positionals = [arg for arg in args if arg not in _FLAGS]
    job_path, resp_path = None, None
    if positionals:
        first = positionals[0]
        if first.lower().endswith(".json") and os.path.isfile(first):
            job_path = first
            if len(positionals) > 1:
                resp_path = positionals[1]
        elif len(positionals) > 1:
            resp_path = positionals[1]
    return check, job_path, resp_path


def _apply_job(job, warnings):
    global OUT_PRT, RESP
    for key in sorted(set(job) - {"params", "out_prt", "resp"}):
        warnings.append("未知 job 键（已忽略）: %s" % key)
    params = job.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("job.params 必须是对象")
    for key, value in params.items():
        if key in DEFAULT_PARAMS:
            PARAMS[key] = value
        else:
            warnings.append("未知参数（已忽略）: %s" % key)
    if job.get("out_prt"):
        OUT_PRT = str(job["out_prt"])
    if job.get("resp"):
        RESP = str(job["resp"])


def _prepare_outputs():
    for path in (OUT_PRT, RESP):
        folder = os.path.dirname(os.path.abspath(path))
        if folder:
            os.makedirs(folder, exist_ok=True)
        if os.path.exists(path):
            os.remove(path)


def _new_part():
    global SESSION
    SESSION = NXOpen.Session.GetSession()
    builder = SESSION.Parts.FileNew()
    builder.TemplateFileName = "model-plain-1-mm-template.prt"
    builder.NewFileName = OUT_PRT
    builder.MakeDisplayedPart = True
    builder.Units = NXOpen.Part.Units.Millimeters
    builder.Commit()
    builder.Destroy()
    PART["part"] = SESSION.Parts.Work


def run(argv=None):
    """建模或 --check。返回进程退出码。"""
    global OUT_PRT, RESP
    argv = list(sys.argv if argv is None else argv)
    check, job_path, resp_path = _parse_cli(argv[1:])

    warnings = []
    explicit_resp = bool(resp_path)
    if job_path:
        with open(job_path, encoding="utf-8") as handle:
            job = json.load(handle)
        if not isinstance(job, dict):
            raise ValueError("job 文件必须是 JSON 对象")
        _apply_job(job, warnings)
        explicit_resp = explicit_resp or bool(job.get("resp"))
    if resp_path:
        RESP = resp_path

    LOG["params"] = dict(PARAMS)
    errors, param_warnings = validate_params(PARAMS)
    warnings.extend(param_warnings)
    for warning in warnings:
        print("[wormbox] warning: " + warning, file=sys.stderr)

    if check:
        report = {"ok": not errors, "errors": errors,
                  "warnings": warnings, "params": dict(PARAMS)}
        print(json.dumps(report, ensure_ascii=False, indent=1))
        if explicit_resp:
            _write_json(RESP, report)
        for error in errors:
            print("[wormbox] error: " + error, file=sys.stderr)
        return 0 if not errors else 2

    if errors:
        for error in errors:
            print("[wormbox] error: " + error, file=sys.stderr)
        raise ValueError("%d parameter error(s); run with --check for details"
                         % len(errors))
    if NXOpen is None:
        raise RuntimeError("NXOpen 不可用：请在 NX 里用 run_journal 运行，"
                           "或只做参数校验 --check")

    LOG.clear()
    LOG.update({"params": dict(PARAMS), "steps": []})
    _prepare_outputs()
    _new_part()
    build()
    LOG["ok"] = all(item.get("ok") for item in LOG["steps"])
    _write_json(RESP, LOG)
    if not LOG["ok"]:
        print("[wormbox] build finished with errors; see " + RESP, file=sys.stderr)
    return 0 if LOG["ok"] else 1


def _run():
    """顶层兜底：异常也要落盘，便于定位。"""
    try:
        return run()
    except SystemExit:
        raise
    except Exception:
        LOG["ok"] = False
        LOG["fatal"] = traceback.format_exc()
        LOG.setdefault("params", dict(PARAMS))
        if NXOpen is not None:  # 没有 NX 时不要往 Windows 路径写文件
            try:
                _write_json(RESP, LOG)
            except Exception:
                pass
        print(LOG["fatal"], file=sys.stderr)
        return 1


if os.environ.get("WORMBOX_NO_AUTORUN") != "1":
    _status = _run()
    if _status:
        sys.exit(_status)

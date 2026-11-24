#!/usr/bin/env python3
"""生成「流程拓扑全貌」SVG：节点/标签/类型全部取自 ldcs.flow_default.DEFAULT_FLOW，
坐标与走线为本脚本手排（保证正交路由不压框）。

排版规则（2026-09-15 重排）：
  ① 四条业务支线各占一行，行内列位一致（问 → 抽取 → 判定 → 结论），横向一条线读到底；
  ② 异常链（重试链 / 改口回退 / 超限兜底 / 终态汇聚）各走**独立通道**，任意两条边的线段都不许重叠；
  ③ 图例放最底部通栏，任何线段不得穿越图例区；
  ④ 边集合必须与 DEFAULT_FLOW 一一对应（改流程忘了改图会直接报错）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ldcs.flow_default import DEFAULT_FLOW  # noqa: E402

DST = Path(__file__).resolve().parent / "assets" / "流程拓扑图.svg"

W, H = 156, 50          # 普通节点尺寸
SW, SH = 92, 44         # 起止节点尺寸（start）
EW, EH = 96, 140        # 终止节点尺寸（end，纵向长条：终态线各占一个落点）

CANVAS_W, CANVAS_H = 1920, 1342
LEGEND = (24, 1200, 1872, 122)   # 图例框：x, y, w, h

# ---- 手排坐标（左上角）--------------------------------------------------
# 列位：route 136 → 问 320 → 抽取 550 → 判定/缺项 760 → 计算/分类 1010 → 结论 1400
# 行位：户籍 170 / 性别年龄 400 / 退休 630 / 失业金 860（重试节点统一 +118）
POS = {
    "start":            (24, 463),
    "route_intent":     (136, 460),

    # 支线 1：户籍（含分类）
    "ask_huji":         (320, 170),
    "ask_huji_retry":   (320, 288),
    "extract_huji":     (550, 170),
    "missing_huji":     (760, 170),
    "huji_attempt":     (760, 288),
    "classify_huji":    (1010, 170),
    "conclude_gz":      (1400, 170),
    "conclude_gd":      (1400, 290),

    # 支线 2：性别年龄
    "ask_ga":           (320, 400),
    "ask_ga_retry":     (320, 518),
    "extract_ga":       (550, 400),
    "check_ga":         (760, 400),
    "ga_attempt":       (760, 518),
    "judge_age":        (1010, 400),
    "conclude_q":       (1400, 400),
    "conclude_u":       (1400, 520),

    # 支线 3：退休时间
    "ask_retire":       (320, 630),
    "ask_retire_retry": (320, 748),
    "extract_retire":   (550, 630),
    "check_retire":     (760, 630),
    "retire_attempt":   (760, 748),
    "calc_retire":      (1010, 630),
    "conclude_retire":  (1400, 630),

    # 支线 4：失业保险金
    "ask_unemp":        (320, 860),
    "ask_unemp_retry":  (320, 978),
    "extract_unemp":    (550, 860),
    "check_unemp":      (760, 860),
    "unemp_attempt":    (760, 978),
    "calc_unemp":       (1010, 860),
    "conclude_unemp":   (1400, 860),

    # 顶部三个非业务出口
    "off_topic":        (900, 100),
    "followup":         (1120, 100),
    "guide_ss":         (1340, 100),

    # 改口回退 / 兜底
    "fix_route":        (760, 1100),
    "fallback":         (1400, 1100),

    "end_node":         (1800, 420),
}

STYLE = {  # 类型 → (填充, 描边, 文字色, 角标)
    "start":     ("#e9f6ea", "#a8d5ab", "#1b5e20", "开始"),
    "say":       ("#e9f1fb", "#b7d3ef", "#124f8c", "话术"),
    "extract":   ("#f1f0fd", "#d3d0f4", "#574ec4", "抽取"),
    "condition": ("#fdf0ea", "#f5d3c3", "#b8481f", "判定"),
    "code":      ("#f7eceb", "#e8c9c5", "#8f2727", "代码"),
    "tool":      ("#fdeef5", "#f5cde0", "#a8386a", "工具"),
    "end":       ("#f1f1f0", "#d8d8d4", "#4a4945", "结束"),
}

# 边类型 → (描边色, 线宽, dash, 是否带箭头)
EDGE = {
    "main":   ("#9aa3b0", 1.7, "",        True),
    "fast":   ("#b9c1cc", 1.6, "6 4",     True),
    "retry":  ("#e08a4a", 1.6, "5 3",     True),
    "fix":    ("#7f77dd", 1.6, "5 3",     True),
    "giveup": ("#c0514a", 1.5, "3 3",     True),
    "term":   ("#5aa469", 1.6, "",        True),
}


def size_of(nid, n):
    if n["type"] == "start":
        return SW, SH
    if n["type"] == "end":
        return EW, EH
    return W, H


# ---- 连边：(from, to, 类型, 标签, 折点…, 是否画箭头[, 标签位置(x,y,anchor)]) ----
# 通道约定（互不重叠）：
#   快路径竖线   x = 484 / 492 / 500 / 508   （问 与 抽取 之间的走廊）
#   「再答」竖线 x = 420                     （从重试节点上沿起，各支线独立一行）
#   改口回退竖线 x = 924 / 932 / 940 / 948   （判定列与计算列之间的走廊）
#   超限兜底竖线 x = 962 / 972 / 982 / 992   （同上）
#   终态汇聚竖线 x = 1600+，顶部三出口从 end 上沿落
E = [
    # ===== 入口与意图分流 =====
    ("start", "route_intent", "main", "", [(116, 485), (136, 485)], 1),
    ("route_intent", "ask_huji", "main", "命中", [(230, 460), (230, 160), (286, 160), (286, 195), (320, 195)], 1),
    ("route_intent", "ask_retire", "main", "退休时间", [(160, 510), (160, 655), (320, 655)], 1),
    ("route_intent", "ask_unemp", "main", "失业金", [(180, 510), (180, 885), (320, 885)], 1),
    ("route_intent", "off_topic", "main", "无关", [(170, 460), (170, 56), (978, 56), (978, 100)], 1),
    ("route_intent", "followup", "main", "已完成·追问", [(190, 460), (190, 72), (1198, 72), (1198, 100)], 0),
    ("route_intent", "guide_ss", "main", "查询指引", [(210, 460), (210, 88), (1418, 88), (1418, 100)], 1),

    # ===== 快路径：上一轮已在答某支线 → 直连抽取 =====
    ("route_intent", "extract_huji", "fast", "上一轮在答·户籍", [(292, 462), (484, 462), (484, 150), (620, 150), (620, 170)], 1, (477, 250, "end")),
    ("route_intent", "extract_ga", "fast", "上一轮在答·性别年龄", [(292, 474), (492, 474), (492, 380), (620, 380), (620, 400)], 1, (485, 462, "end")),
    ("route_intent", "extract_retire", "fast", "上一轮在答·出生年月", [(292, 486), (500, 486), (500, 610), (620, 610), (620, 630)], 1, (493, 580, "end")),
    ("route_intent", "extract_unemp", "fast", "上一轮在答·缴费时长", [(292, 498), (508, 498), (508, 840), (620, 840), (620, 860)], 1, (501, 810, "end")),

    # ===== 支线 1：户籍 =====
    ("ask_huji", "extract_huji", "main", "", [(476, 195), (550, 195)], 1),
    ("extract_huji", "fix_route", "fix", "", [(706, 195), (733, 195), (733, 1104), (760, 1104)], 1),
    ("missing_huji", "classify_huji", "main", "齐全", [(916, 195), (1010, 195)], 1),
    ("missing_huji", "huji_attempt", "retry", "缺失", [(838, 220), (838, 288)], 1, (831, 258, "end")),
    ("missing_huji", "ask_huji", "retry", "首问", [(800, 170), (800, 140), (430, 140), (430, 170)], 1),
    ("huji_attempt", "ask_huji_retry", "retry", "重试", [(760, 313), (476, 313)], 1),
    ("huji_attempt", "fallback", "giveup", "重试超限", [(916, 313), (962, 313), (962, 1104), (1400, 1104)], 1, (1030, 1116, "middle")),
    ("ask_huji_retry", "extract_huji", "retry", "再答", [(420, 288), (420, 226), (620, 226), (620, 220)], 1),
    ("classify_huji", "conclude_gz", "main", "广州", [(1166, 195), (1400, 195)], 1),
    ("classify_huji", "conclude_gd", "main", "省内非广州", [(1166, 215), (1280, 215), (1280, 315), (1400, 315)], 1),
    ("classify_huji", "check_ga", "main", "省外 → 继续测算", [(1088, 220), (1088, 350), (838, 350), (838, 400)], 1),

    # ===== 支线 2：性别年龄 =====
    ("ask_ga", "extract_ga", "main", "", [(476, 425), (550, 425)], 1),
    ("extract_ga", "fix_route", "fix", "", [(706, 425), (741, 425), (741, 1114), (760, 1114)], 0),
    ("check_ga", "judge_age", "main", "齐全", [(916, 425), (1010, 425)], 1),
    ("check_ga", "ga_attempt", "retry", "缺失", [(838, 450), (838, 518)], 1, (831, 488, "end")),
    ("check_ga", "ask_ga", "retry", "首问", [(800, 400), (800, 370), (430, 370), (430, 400)], 1),
    ("ga_attempt", "ask_ga_retry", "retry", "重试", [(760, 543), (476, 543)], 1),
    ("ga_attempt", "fallback", "giveup", "重试超限", [(916, 543), (972, 543), (972, 1114), (1400, 1114)], 0, (1120, 1130, "middle")),
    ("ask_ga_retry", "extract_ga", "retry", "再答", [(420, 518), (420, 456), (620, 456), (620, 450)], 1),
    ("judge_age", "conclude_q", "main", "符合", [(1166, 415), (1280, 415), (1280, 425), (1400, 425)], 1),
    ("judge_age", "conclude_u", "main", "超龄", [(1166, 435), (1280, 435), (1280, 545), (1400, 545)], 1),

    # ===== 支线 3：退休时间 =====
    ("ask_retire", "extract_retire", "main", "", [(476, 655), (550, 655)], 1),
    ("extract_retire", "fix_route", "fix", "", [(706, 655), (749, 655), (749, 1124), (760, 1124)], 0),
    ("check_retire", "calc_retire", "main", "齐全", [(916, 655), (1010, 655)], 1),
    ("check_retire", "retire_attempt", "retry", "缺失", [(838, 680), (838, 748)], 1, (831, 718, "end")),
    ("check_retire", "ask_retire", "retry", "首问", [(800, 630), (800, 600), (430, 600), (430, 630)], 1),
    ("retire_attempt", "ask_retire_retry", "retry", "重试", [(760, 773), (476, 773)], 1),
    ("retire_attempt", "fallback", "giveup", "重试超限", [(916, 773), (982, 773), (982, 1124), (1400, 1124)], 0, (1210, 1144, "middle")),
    ("ask_retire_retry", "extract_retire", "retry", "再答", [(420, 748), (420, 686), (620, 686), (620, 680)], 1),
    ("calc_retire", "conclude_retire", "main", "", [(1166, 655), (1400, 655)], 1),

    # ===== 支线 4：失业保险金 =====
    ("ask_unemp", "extract_unemp", "main", "", [(476, 885), (550, 885)], 1),
    ("extract_unemp", "fix_route", "fix", "", [(706, 885), (757, 885), (757, 1134), (760, 1134)], 1),
    ("check_unemp", "calc_unemp", "main", "齐全", [(916, 885), (1010, 885)], 1),
    ("check_unemp", "unemp_attempt", "retry", "缺失", [(838, 910), (838, 978)], 1, (831, 948, "end")),
    ("check_unemp", "ask_unemp", "retry", "首问", [(800, 860), (800, 830), (430, 830), (430, 860)], 1),
    ("unemp_attempt", "ask_unemp_retry", "retry", "重试", [(760, 1003), (476, 1003)], 1),
    ("unemp_attempt", "fallback", "giveup", "重试超限", [(916, 1003), (992, 1003), (992, 1134), (1400, 1134)], 1, (1300, 1158, "middle")),
    ("ask_unemp_retry", "extract_unemp", "retry", "再答", [(420, 978), (420, 916), (620, 916), (620, 910)], 0),
    ("calc_unemp", "conclude_unemp", "main", "", [(1166, 885), (1400, 885)], 1),

    # ===== 改口回退：fix_route → 各支线重判（标签钉在行间空隙，压在通道线上）=====
    ("fix_route", "missing_huji", "fix", "回户籍重判", [(916, 1110), (924, 1110), (924, 214), (858, 214), (858, 220)], 1, (920, 250, "end")),
    ("fix_route", "check_ga", "fix", "回性别年龄", [(916, 1120), (932, 1120), (932, 444), (858, 444), (858, 450)], 0, (928, 490, "end")),
    ("fix_route", "check_retire", "fix", "回退休", [(916, 1130), (940, 1130), (940, 674), (858, 674), (858, 680)], 0, (936, 720, "end")),
    ("fix_route", "check_unemp", "fix", "回失业金", [(916, 1140), (948, 1140), (948, 904), (858, 904), (858, 910)], 1, (944, 940, "end")),

    # ===== 终态汇聚 → end（业务结论从左侧落，非业务出口从上沿落）=====
    ("conclude_gz", "end_node", "term", "", [(1556, 195), (1654, 195), (1654, 440), (1800, 440)], 1),
    ("conclude_gd", "end_node", "term", "", [(1556, 315), (1672, 315), (1672, 458), (1800, 458)], 0),
    ("conclude_q", "end_node", "term", "", [(1556, 425), (1690, 425), (1690, 476), (1800, 476)], 0),
    ("conclude_u", "end_node", "term", "", [(1556, 545), (1708, 545), (1708, 494), (1800, 494)], 0),
    ("conclude_retire", "end_node", "term", "", [(1556, 655), (1726, 655), (1726, 512), (1800, 512)], 0),
    ("conclude_unemp", "end_node", "term", "", [(1556, 885), (1744, 885), (1744, 530), (1800, 530)], 0),
    ("fallback", "end_node", "term", "", [(1556, 1125), (1762, 1125), (1762, 548), (1800, 548)], 1),
    ("off_topic", "end_node", "term", "", [(978, 150), (978, 390), (1812, 390), (1812, 420)], 1),
    ("followup", "end_node", "term", "", [(1276, 125), (1276, 376), (1836, 376), (1836, 420)], 0),
    ("guide_ss", "end_node", "term", "", [(1496, 125), (1566, 125), (1566, 362), (1860, 362), (1860, 420)], 0),
]


def poly(pts, r=9):
    d = f"M {pts[0][0]},{pts[0][1]}"
    for i in range(1, len(pts) - 1):
        p0, p1, p2 = pts[i - 1], pts[i], pts[i + 1]
        v1 = (p1[0] - p0[0], p1[1] - p0[1])
        v2 = (p2[0] - p1[0], p2[1] - p1[1])
        l1 = max(abs(v1[0]) + abs(v1[1]), 1)
        l2 = max(abs(v2[0]) + abs(v2[1]), 1)
        rr = min(r, l1 / 2, l2 / 2)
        a = (p1[0] - v1[0] / l1 * rr, p1[1] - v1[1] / l1 * rr)
        b = (p1[0] + v2[0] / l2 * rr, p1[1] + v2[1] / l2 * rr)
        d += f" L {a[0]:.1f},{a[1]:.1f} Q {p1[0]},{p1[1]} {b[0]:.1f},{b[1]:.1f}"
    d += f" L {pts[-1][0]},{pts[-1][1]}"
    return d


def _overlap(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def label_box(text, x, y, anc):
    """标签占位框（用于避让节点框与互相重叠检查）。"""
    tw = max(34, sum(12 if ord(c) > 255 else 6.4 for c in text) + 6)
    if anc == "middle":
        return (x - tw / 2, y - 12, x + tw / 2, y + 4)
    if anc == "end":
        return (x - tw, y - 12, x, y + 4)
    return (x, y - 12, x + tw, y + 4)


def label_at(pts, text, bx):
    """标签落在最长的那一段中点；若压到节点框，换一侧、再换次长段。"""
    order = sorted(range(len(pts) - 1),
                   key=lambda i: -(abs(pts[i + 1][0] - pts[i][0]) + abs(pts[i + 1][1] - pts[i][1])))
    fallback = None
    for i in order:
        a, b = pts[i], pts[i + 1]
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        horiz = a[1] == b[1]
        for above in (False, True):
            if horiz:
                cx, cy, anc = mx, my + (-6 if above else 7), "middle"
            else:
                cx, cy, anc = mx + (-7 if above else 7), my + 4, ("end" if above else "start")
            box = label_box(text, cx, cy, anc)
            if fallback is None:
                fallback = (cx, cy, anc, False)
            if not any(_overlap(box, r) for r in bx.values()):
                return cx, cy, anc, True
    return fallback


def boxes(nodes):
    out = {}
    for nid, n in nodes.items():
        x, y = POS[nid]
        w, h = size_of(nid, n)
        out[nid] = (x, y, x + w, y + h)
    return out


def _owning(boxes_, pt):
    for nid, (x0, y0, x1, y1) in boxes_.items():
        if (abs(pt[0] - x0) <= 1.5 or abs(pt[0] - x1) <= 1.5) and y0 - 1.5 <= pt[1] <= y1 + 1.5:
            return nid
        if (abs(pt[1] - y0) <= 1.5 or abs(pt[1] - y1) <= 1.5) and x0 - 1.5 <= pt[0] <= x1 + 1.5:
            return nid
    return None


def _legend_box():
    x, y, w, h = LEGEND
    return (x, y, x + w, y + h)


def audit(nodes):
    """自检：①边集合与流程一致 ②端点落框 ③不压框 ④两条边不共用同一段线 ⑤不穿图例区。"""
    bx = boxes(nodes)
    bad = []

    flow_edges = {(e["from"], e["to"]) for e in DEFAULT_FLOW["edges"]}
    draw_edges = {(f, t) for f, t, *_ in E}
    if flow_edges - draw_edges:
        bad.append(f"图中缺失的边：{sorted(flow_edges - draw_edges)}")
    if draw_edges - flow_edges:
        bad.append(f"图中多画的边：{sorted(draw_edges - flow_edges)}")

    for f, t, kind, _, pts, arrow, *_ in E:
        src, dst = _owning(bx, pts[0]), _owning(bx, pts[-1])
        if not src or (arrow and not dst):
            bad.append(f"{f}→{t} 端点未落在节点边框 src={src} dst={dst} {pts[0]}→{pts[-1]}")
        for a, b in zip(pts, pts[1:]):
            assert a[0] == b[0] or a[1] == b[1], f"{f}→{t} 非正交线段 {a}→{b}"
            for nid, (x0, y0, x1, y1) in bx.items():
                if nid in (src, dst):
                    continue
                if a[1] == b[1]:
                    if not (y0 - 2 < a[1] < y1 + 2):
                        continue
                    lo, hi = min(a[0], b[0]), max(a[0], b[0])
                    ov = min(hi, x1) - max(lo, x0)
                else:
                    if not (x0 - 2 < a[0] < x1 + 2):
                        continue
                    lo, hi = min(a[1], b[1]), max(a[1], b[1])
                    ov = min(hi, y1) - max(lo, y0)
                if ov > 6:
                    bad.append(f"{f}→{t} 压框：穿过「{nodes[nid]['label']}」重叠 {ov:.0f}px")

    segs = []
    for i, (f, t, _, _, pts, _, *_) in enumerate(E):
        for a, b in zip(pts, pts[1:]):
            segs.append((i, f, t, a, b))
    for m in range(len(segs)):
        for n in range(m + 1, len(segs)):
            i1, f1, t1, a1, b1 = segs[m]
            i2, f2, t2, a2, b2 = segs[n]
            if i1 == i2:
                continue
            if a1[1] == b1[1] and a2[1] == b2[1] and abs(a1[1] - a2[1]) <= 2:
                ov = min(max(a1[0], b1[0]), max(a2[0], b2[0])) - max(min(a1[0], b1[0]), min(a2[0], b2[0]))
            elif a1[0] == b1[0] and a2[0] == b2[0] and abs(a1[0] - a2[0]) <= 2:
                ov = min(max(a1[1], b1[1]), max(a2[1], b2[1])) - max(min(a1[1], b1[1]), min(a2[1], b2[1]))
            else:
                continue
            if ov > 6:
                bad.append(f"线段重叠 {ov:.0f}px：{f1}→{t1} 与 {f2}→{t2}")

    lg = _legend_box()
    for f, t, _, _, pts, _, *_ in E:
        for a, b in zip(pts, pts[1:]):
            if _overlap((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])), lg):
                bad.append(f"{f}→{t} 穿过图例区")
    return bad


def main():
    nodes = {n["id"]: n for n in DEFAULT_FLOW["nodes"]}
    missing = set(nodes) - set(POS)
    assert not missing, f"POS 缺坐标: {missing}"
    extra = set(POS) - set(nodes)
    assert not extra, f"POS 多出未定义节点: {extra}"

    bad = audit(nodes)
    bx = boxes(nodes)
    if bad:
        print("⚠ 几何自检发现 %d 处问题：" % len(bad))
        for b in bad:
            print("  -", b)
    else:
        print("✓ 几何自检通过：无压框、无重叠、不穿图例、端点均落框")

    out = []
    add = out.append
    add(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CANVAS_W} {CANVAS_H}" '
        f'width="{CANVAS_W}" height="{CANVAS_H}" role="img" aria-label="多轮业务问答流程拓扑全貌">')
    add("<defs><style>"
        ".nd{stroke-width:1.2}"
        ".nm{font:600 12.5px -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;text-anchor:middle}"
        ".bd{font:500 9px -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;text-anchor:end;opacity:.62}"
        ".el{font:500 10.5px -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;"
        "fill:#69737f;paint-order:stroke;stroke:#ffffff;stroke-width:3.4;stroke-linejoin:round}"
        ".lg{font:400 11.5px -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;fill:#3d444d}"
        ".lgt{font:700 11px -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;letter-spacing:.1em;fill:#6b7480}"
        ".row{font:600 11.5px -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;fill:#a6adb8}"
        "</style>")
    for k, (c, _, _, _) in EDGE.items():
        add(f'<marker id="ar-{k}" viewBox="0 0 10 10" refX="9.2" refY="5" markerWidth="7" '
            f'markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>')
    add("</defs>")
    add(f'<rect width="{CANVAS_W}" height="{CANVAS_H}" fill="#ffffff"/>')
    add(f'<text class="lgt" x="24" y="26">流程拓扑全貌 · {len(nodes)} 个节点 / {len(E)} 条边</text>')

    # ---- 左侧支线行标：四条支线一眼分得开 ----
    for label, y in (("户籍", 170), ("性别年龄", 400), ("退休时间", 630), ("失业保险金", 860)):
        add(f'<text class="row" x="24" y="{y + 30}">{label}</text>')

    # ---- 边（先画线，节点压在上面）----
    squeezed = []
    label_boxes = []
    for f, t, kind, lab, pts, arrow, *rest in E:
        color, wid, dash, _ = EDGE[kind]
        d = poly(pts)
        mk = f' marker-end="url(#ar-{kind})"' if arrow else ""
        da = f' stroke-dasharray="{dash}"' if dash else ""
        add(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{wid}"'
            f'{da}{mk} stroke-linecap="round"/>')
        if lab:
            if rest and rest[0]:
                lx, ly, anc = rest[0]
                ok = True
            else:
                lx, ly, anc, ok = label_at(pts, lab, bx)
                if not ok:
                    squeezed.append(lab)
                lx, ly = round(lx), round(ly)
            label_boxes.append((lab, label_box(lab, lx, ly, anc)))
            add(f'<text class="el" x="{lx}" y="{ly}" text-anchor="{anc}">{lab}</text>')
    if squeezed:
        print("⚠ 以下标签无空位、已就近放置：", "、".join(squeezed))
    for m in range(len(label_boxes)):
        for n in range(m + 1, len(label_boxes)):
            if _overlap(label_boxes[m][1], label_boxes[n][1]):
                print(f"⚠ 标签互相压：{label_boxes[m][0]} ↔ {label_boxes[n][0]}")

    # ---- 节点 ----
    for nid, n in nodes.items():
        x, y = POS[nid]
        w, h = size_of(nid, n)
        fill, stroke, ink, badge = STYLE[n["type"]]
        add(f'<rect class="nd" x="{x}" y="{y}" width="{w}" height="{h}" rx="9" '
            f'fill="{fill}" stroke="{stroke}"/>')
        add(f'<text class="nm" x="{x + w / 2:.0f}" y="{y + h / 2 + 4.5:.0f}" fill="{ink}">{n["label"]}</text>')
        add(f'<text class="bd" x="{x + w - 8}" y="{y + 13}" fill="{ink}">{badge}</text>')

    # ---- 图例（最底部通栏，不与任何走线相交）----
    lx, ly, lw, lh = LEGEND
    add(f'<rect x="{lx}" y="{ly}" width="{lw}" height="{lh}" rx="10" fill="#fbfcfe" stroke="#e6e9ee"/>')
    add(f'<text class="lgt" x="{lx + 18}" y="{ly + 22}">图例</text>')
    for i, (t, (fill, stroke, ink, name)) in enumerate(STYLE.items()):
        cx = lx + 18 + (i % 7) * 260
        cy = ly + 34
        add(f'<rect x="{cx}" y="{cy}" width="14" height="12" rx="3" fill="{fill}" stroke="{stroke}"/>')
        add(f'<text class="lg" x="{cx + 21}" y="{cy + 11}">{name}</text>')
    for i, (k, lab) in enumerate([("main", "正常出口"), ("fast", "快路径·直连抽取"),
                                  ("retry", "缺失→重试链"), ("fix", "改口回退"),
                                  ("giveup", "重试超限→兜底"), ("term", "终态汇聚")]):
        color, _, dash, _ = EDGE[k]
        cx = lx + 18 + (i % 6) * 300
        cy = ly + 84
        da = f' stroke-dasharray="{dash}"' if dash else ""
        add(f'<line x1="{cx}" y1="{cy}" x2="{cx + 26}" y2="{cy}" stroke="{color}" '
            f'stroke-width="1.8"{da}/>')
        add(f'<text class="lg" x="{cx + 34}" y="{cy + 4}">{lab}</text>')

    add("</svg>")
    DST.write_text("\n".join(out), encoding="utf-8")
    print("written:", DST, len("\n".join(out)), "bytes | nodes:", len(nodes), "edges:", len(E))


if __name__ == "__main__":
    main()

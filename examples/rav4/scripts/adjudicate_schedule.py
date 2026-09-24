"""双通道终审合并：几何通道 + VLM 盲读 → 权威版保养计划 JSON。

裁决规则（依据 2026-09-24 物证终审，见 adjudication_notes.md）：
- actions / months / months_detail：几何通道为准
  （所有争议行字母 x 与列锚点距离 Δ0.0，物理布局无可争议；
    VLM 4B 存在系统性错列，如 10/30/50/70 vs 真实 20/40/60/80）
- name / note：VLM 为准（几何通道的行装配在名称行/折行三态上有系统性弱点；
  VLM 缺失时保留几何值）
- 附加表：几何通道的条件分组结构为准（A-1/A-2/A-3/B-1），
  间隔文本与 VLM 对拍，不一致 → REVIEW
- months 与 months_detail 互为等价编码（月数列写 R：24 时
  geo 记 md={R:24}、VLM 记 months=24），不算冲突

输出:
  workdir/gasoline/specs_maintenance_schedule.final.json
  workdir/gasoline/adjudication_notes.md （裁决记录）
"""
import difflib
import json
import re
import sys
from pathlib import Path

GDIR = None  # main() 里按变体赋值

NAME_NOISE = re.compile(r"<+.*?>+|《参见注.*?》?|释\s*\d*。?>*" )
MONTHS_DASH = ("–", "—", "-")


def norm_name(s):
    return NAME_NOISE.sub("", s or "").replace(" ", "").replace("　", "")


def norm_months(m):
    if m is None:
        return None
    if isinstance(m, str):
        if m.strip() in MONTHS_DASH:
            return "–"
        d = re.sub(r"\D", "", m)
        return int(d) if d else None
    return int(m)


def eff_months(item):
    """月数等价归一：months_detail 存在时以 {代码:月数} 表示，否则 months。"""
    md = item.get("months_detail") or {}
    m = norm_months(item.get("months"))
    if md:
        return ("md", tuple(sorted((str(k).upper(), v) for k, v in md.items())))
    if m is not None:
        return ("m", m)
    return None


def norm_interval(s):
    s = re.sub(r"\s", "", s or "").replace(":", "：")  # 全半角冒号统一
    s = re.sub(r"(?<=个月)\d+(?=[IR]：)", "", s)       # 表内脚注序号
    s = re.sub(r"(?<=个月)\d+$", "", s)                # 行尾脚注序号
    return s


def main():
    """按字段来源规则合并两通道结果，并输出未解决冲突的审计记录。"""
    global GDIR
    variant = sys.argv[1] if len(sys.argv) > 1 else ""
    if variant not in ("gasoline", "hybrid"):
        print("用法: adjudicate_schedule.py gasoline|hybrid")
        return 2
    GDIR = Path(__file__).resolve().parents[1] / "workdir" / variant
    geo = json.loads((GDIR / "specs_maintenance_schedule.json").read_text(encoding="utf-8"))
    vlm = json.loads((GDIR / "vlm_schedule.json").read_text(encoding="utf-8"))

    # ---- 主表合并 ----
    vlm_items = {}
    for c in vlm.get("categories", []):
        for it in c.get("items", []):
            if isinstance(it.get("no"), int):
                vlm_items[it["no"]] = (c.get("name", ""), it)

    notes, review = [], []
    KNOWN_CATS = {"发动机基本部件", "点火系统", "燃油和排放控制系统", "底盘和车身", "电气系统"}
    cats_out = []
    for c in geo["categories"]:
        items_out = []
        for g in c["items"]:
            no = g["no"]
            out = dict(g)
            if no in vlm_items:
                _, v = vlm_items[no]
                vn = norm_name(v.get("name")).strip("》>\"“”")
                gn = norm_name(g.get("name"))
                if vn:
                    r = difflib.SequenceMatcher(None, gn, vn).ratio() if gn else 0.0
                    prev_vn = norm_name(vlm_items.get(no - 1, ("", {}))[1].get("name")) \
                        if vlm_items.get(no - 1) else ""
                    geo_is_junk = (not gn) or gn in KNOWN_CATS or (
                        len(gn.rstrip("）(（")) <= 5
                        and gn.rstrip("）(（") in vn)  # 折行残片/分类名泄漏
                    if not geo_is_junk and prev_vn and difflib.SequenceMatcher(
                            None, gn, prev_vn).ratio() >= 0.5:
                        geo_is_junk = True  # geo 名实为上一条目名称的折行尾
                        notes.append(f"#{no} geo 名为 #{no-1} 名称折行尾，判残片")
                    if r >= 0.3 or geo_is_junk:
                        if gn != vn:
                            notes.append(f"#{no} 名称取 VLM: {gn or '（空）'} → {vn}")
                        out["name"] = vn
                    else:
                        review.append(f"#{no} 名称两通道差异过大，保留几何值待查: geo={gn} vlm={vn}")
                # note：取更长且包含对方的版本；geo 无则取 VLM
                vn_note, gn_note = (v.get("note") or ""), (out.get("note") or "")
                if vn_note and gn_note and gn_note in vn_note and len(vn_note) > len(gn_note):
                    out["note"] = vn_note
                    notes.append(f"#{no} note 取 VLM 长版: {vn_note[:30]}")
                elif vn_note and not gn_note:
                    out["note"] = vn_note
                    notes.append(f"#{no} note 取 VLM: {vn_note[:30]}")
            # note 清理：去掉 <<参见注释N。>> 外壳 / 剔除与名称重复的假 note
            if out.get("note"):
                out["note"] = re.sub(r"[<《]+\s*参见注?释?\s*(\d+)[。.]?\s*[>》]*",
                                     r"参见注释\1", out["note"]).strip()
                nn = norm_name(out["note"])
                if not nn or re.fullmatch(r"[A-Za-z0-9]+", out["note"].strip()) \
                        or nn in norm_name(out["name"]):
                    out.pop("note")
            # months 与单码 md 同值时去重（如 月:6 + md{I:6}）
            md = out.get("months_detail") or {}
            mm = norm_months(out.get("months"))
            if md and len(md) == 1 and isinstance(mm, int) and list(md.values())[0] == mm:
                out.pop("months_detail", None)
            out.pop("ri", None)
            items_out.append(out)
        cats_out.append({"name": c["name"], "items": items_out})

    # ---- VLM 独有条目（几何通道整条漏读）→ 补入 ----
    geo_nos = {it["no"] for c in cats_out for it in c["items"]}
    added = []
    for no in sorted(set(vlm_items) - geo_nos):
        _, v = vlm_items[no]
        entry = {"no": no, "name": norm_name(v.get("name")).strip("》>"),
                 "actions": {str(int(k)): str(val).upper()[:1]
                             for k, val in (v.get("actions") or {}).items()
                             if str(val).strip().upper()[:1] in "IRT"},
                 "source": "vlm_only"}
        m = norm_months(v.get("months"))
        if m is not None:
            entry["months"] = m
        if v.get("note"):
            entry["note"] = v["note"]
        added.append((no, entry))
        notes.append(f"#{no} 仅 VLM 读出，整条补入: {entry['name']}")
    for no, entry in added:
        # 用编号恢复原表顺序；仅 VLM 读出的条目仍保留来源标记。
        pos_cat, pos_idx = None, 0
        for ci, c in enumerate(cats_out):
            for ii, it in enumerate(c["items"]):
                if it["no"] < no:
                    pos_cat, pos_idx = ci, ii + 1
        if pos_cat is None:
            cats_out[0]["items"].insert(0, entry)
        else:
            cats_out[pos_cat]["items"].insert(pos_idx, entry)

    # ---- 附加表：geo 结构为准，VLM 对拍间隔 ----
    vlm_add = vlm.get("additional") or []
    add_out = []
    for grp in geo.get("additional_schedule", []):
        items_out = []
        for it in grp["items"]:
            out = dict(it)
            cand = [(difflib.SequenceMatcher(
                        None, norm_name(it["name"]),
                        norm_name(v.get("name", ""))).ratio(), v)
                    for v in vlm_add if norm_name(v.get("name", ""))]
            cand = [(r, v) for r, v in cand if r > 0.6]
            cand.sort(key=lambda p: -p[0])  # 空调滤清器/空气滤清器同名碰撞取最像的
            hit = None
            # 附加表同名项目可能出现在多个条件组，先匹配条件再比较间隔。
            for _, v in cand:
                vm = re.match(r"([AB]-\d+)", v.get("condition") or "")
                if vm and vm.group(1) == grp["condition"]:
                    hit = v
                    break
            hit = hit or (cand[0][1] if cand else None)
            if hit:
                gi, vi = norm_interval(it["interval"]), norm_interval(hit.get("interval"))
                if gi != vi and vi:
                    if gi and vi in gi:
                        # geo 混入了相邻条目的错位片段（如“每隔48个月”）→ 取 VLM
                        notes.append(
                            f"附加[{grp['condition']}] {norm_name(it['name'])[:14]} "
                            f"间隔含错位片段，取 VLM: {vi[:32]}")
                        out["interval"] = hit["interval"]
                    elif gi in vi:
                        # geo 跨页/折行丢尾 → 取 VLM 长版
                        out["interval"] = hit["interval"]
                        notes.append(
                            f"附加[{grp['condition']}] {norm_name(it['name'])[:14]} 间隔取 VLM 长版")
                    else:
                        review.append(
                            f"附加[{grp['condition']}] {norm_name(it['name'])[:14]} 间隔不一致: "
                            f"geo={gi[:26]} vlm={vi[:26]}")
            items_out.append(out)
        add_out.append({"condition": grp["condition"], "items": items_out})

    final = {"table": geo["table"], "code_legend": geo["code_legend"],
             "verified": {"method": "几何(bbox)×VLM(qwen3-vl-4b)双通道终审",
                          "date": "2026-09-24",
                          "actions_source": "geometry (Δ0.0 物证)",
                          "name_source": "VLM (行装配更可靠)"},
             "categories": cats_out, "additional_schedule": add_out,
             "unresolved": review}
    outp = GDIR / "specs_maintenance_schedule.final.json"
    outp.write_text(json.dumps(final, ensure_ascii=False, indent=1), encoding="utf-8")

    md = ["# 保养计划表 双通道终审记录", "",
          "- 几何通道: pdftotext -bbox 词级坐标（列锚点 Δ0.0 物证）",
          "- VLM 通道: qwen3-vl-4b-instruct 盲读 p318-321 图片", "",
          "## 裁决依据", "",
          "1. actions/months 一律几何: 所有争议行字母 x 与列锚点距离实测 Δ0.0；",
          "   VLM 系统性错列（把 20/40/60/80 读成 10/30/50/70），且漏读无规律。",
          "2. 名称一律 VLM: 几何行装配在「名称行/折行/分类行」三态歧义上有系统性",
          "   弱点（#5/9/13/22/23/25 残缺），VLM 全部读全。",
          "3. 附加表结构以几何为准: A-1/A-2/A-3/B-1 条件分组为物证重建；",
          "   同名项目跨条件重复出现（如 紧固传动轴螺栓×4），两通道平铺都会错。",
          "4. months 与 months_detail 为等价编码，不判冲突。", "",
          "## 已知局限", "",
          "- #25 后差速器油：月注 I：12 混在名称折行、R：48 在下一行折行中，",
          "  两通道均未捕获 months_detail；语义上大概率与 #24 分动器油相同",
          "  （I:12月/R:48月），但未编造，留空待查原文确认。", "",
          "## 合并记录"] + [f"- {n}" for n in notes] + ["", "## 待人工复查"] + \
        ([f"- {r}" for r in review] or ["- （无）"])
    (GDIR / "adjudication_notes.md").write_text("\n".join(md), encoding="utf-8")

    n = sum(len(c["items"]) for c in cats_out)
    print(f"终审完成: 主表 {n} 项 / 附加 {sum(len(g['items']) for g in add_out)} 项"
          f"（{len(add_out)} 条件组）→ {outp}")
    print(f"字段级合并 {len(notes)} 处; 待人工复查 {len(review)} 处")
    for r in review:
        print(f"  REVIEW: {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

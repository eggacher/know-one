"""定期保养表 → 结构化 JSON。

技术路线（几何对齐）：
- pdftotext -bbox 词级坐标，按 y 聚类重建视觉行；
- 表头 “10 20 30 …” 数字 x 中心 = 列锚点；I/R/T 词最近邻配列；
- 两遍法：先标注行角色（表头/条目/月注/噪声），再组装条目，
  解决「分类行 vs 名称行 vs 折行」的歧义：
  * 条目行自身名称为空时，其上方的中文行是名称行；
  * 否则距上方条目更近的中文行是折行续名；
  * 都不是 → 分类。
- x ≥ 列锚点区的非代码词（“每行驶 100000 km （公里）”）→ note；
- 行尾/独立行 “I：6” → months_detail；行尾数字/– → months。

未识别行进 leftovers。
输出: workdir/gasoline/specs_maintenance_schedule.json
"""
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WORKDIR = Path(__file__).resolve().parents[1] / "workdir"
XHTML = "{http://www.w3.org/1999/xhtml}"

FILES = {
    "gasoline": "Rav4用户手册（汽油版）.pdf",
    "hybrid": "RAV4HEV用户手册（混动版）.pdf",
}

Y_TOL = 4.0
LEGEND = re.compile(
    r"^(保养操作|保养间隔|（里程表|数，以|准。）|（公里）|月数|里程表读数|注释|请参见|划项目|\"\"|\d+\.|\d{1,3}\s*7-\d)")
ITEM_NO = re.compile(r"^\d{1,2}$")
CODE = re.compile(r"^[IRT]$")
MONTH_CODE = re.compile(r"^([IRT])：(\d{1,3})$")
MONTH_TAIL = re.compile(r"^(\d{1,3}|–|—|-)$")
CN_TEXT = re.compile(r"[\u4e00-\u9fff]")
NOTE_HINT = re.compile(r"km|公里|完成|包括|首次|检查一次")
KNOWN_CATEGORIES = {"发动机基本部件", "点火系统", "燃油和排放控制系统", "底盘和车身", "电气系统", "其他"}


def page_rows(page: int, pdf: Path) -> list[list[dict]]:
    """按词的纵坐标重建视觉行，再按横坐标恢复每行的列顺序。"""
    out = subprocess.run(
        ["pdftotext", "-bbox", "-f", str(page), "-l", str(page), str(pdf), "-"],
        capture_output=True, text=True, check=True).stdout
    words = []
    for w in ET.fromstring(out).iter(f"{XHTML}word"):
        x0, x1, y = float(w.attrib["xMin"]), float(w.attrib["xMax"]), float(w.attrib["yMin"])
        words.append({"w": w.text or "", "x": (x0 + x1) / 2, "y": y})
    words.sort(key=lambda t: (t["y"], t["x"]))
    rows = []
    for t in words:
        if rows and t["y"] - rows[-1][0]["y"] <= Y_TOL:
            rows[-1].append(t)
        else:
            rows.append([t])
    for r in rows:
        r.sort(key=lambda t: t["x"])
    return rows


def rtext(row):
    return "".join(t["w"] for t in row)


def clean(s):
    s = re.sub(r"[<《《]+参见注?释?\s*\d*[。.]?\s*[>》]*", "", s or "")
    return s.strip("<>").strip()


def parse_page(rows, page, state, add_state, leftovers):
    """解析一页主表；找不到里程表头时转入附加表处理。"""
    # 第一遍只找列锚点并识别行角色，暂不决定中文自由行属于哪个条目。
    anchors = vals = hi = None
    for i, row in enumerate(rows):
        nums = [(t["x"], int(t["w"])) for t in row
                if ITEM_NO.match(t["w"]) and len(t["w"]) == 2]
        if len(nums) >= 5:
            xs = [x for x, _ in nums]
            gaps = [b - a for a, b in zip(xs, xs[1:])]
            # 表头列距大致均匀，可排除正文里偶然出现的一串数字。
            if max(gaps) < 3 * min(gaps):
                anchors, vals, hi = xs, [v for _, v in nums], i
                break
    if anchors is None:
        parse_additional_only(rows, page, add_state, leftovers)
        return
    x_left = anchors[0] - 15          # 名称列右界
    items = []                        # {row_idx, no, name, note, codes, months, md, has_name}
    for ri in range(hi + 1, len(rows)):
        row = rows[ri]
        text = rtext(row)
        if text == "附加保养计划":
            parse_additional(rows[ri:], page, add_state, leftovers)
            break
        if not text or LEGEND.match(text) or text == "保养和维护":
            continue
        if MONTH_CODE.match(text):
            if items and not items[-1].get("free"):
                items[-1]["md"][text[0]] = int(text[2:])
            else:  # 页首孤立月注：作为自由行交给 pending 逻辑（挂到 y 邻近的下一条件）
                items.append({"ri": ri, "text": text, "free": True, "y": row[0]["y"]})
            continue
        words = [t["w"] for t in row]
        if ITEM_NO.match(words[0]) and (any(t["x"] >= x_left for t in row[1:])
                                      or (len(row) > 1 and CN_TEXT.match(row[1]["w"]))):
            codes = [t for t in row if CODE.match(t["w"]) and t["x"] >= x_left]
            note_ws = [t["w"] for t in row
                       if t["x"] >= x_left and not CODE.match(t["w"])
                       and not MONTH_TAIL.match(t["w"]) and not MONTH_CODE.match(t["w"])]
            months = md = None
            tail = words[-1]
            m = MONTH_CODE.match(tail)
            if m:
                md = {m.group(1): int(m.group(2))}
            elif MONTH_TAIL.match(tail):
                months = int(tail) if tail.isdigit() else "–"
            actions = {}
            for t in codes:
                j = min(range(len(anchors)), key=lambda k: abs(anchors[k] - t["x"]))
                if abs(anchors[j] - t["x"]) <= 12:
                    actions[str(vals[j])] = t["w"]
            items.append({"ri": ri, "no": int(words[0]),
                          "name": "".join(t["w"] for t in row[1:] if t["x"] < x_left),
                          "note": "".join(note_ws), "actions": actions,
                          "months": months, "md": md or {}})
            continue
        # 非条目行：中文行/注释行，待 Pass 2 归属
        items.append({"ri": ri, "text": text, "free": True,
                      "y": row[0]["y"]})

    # 第二遍按上下距离及已知分类名归属自由行；不确定的行留在 leftovers。
    cat = state.get("category")
    built, pending_free = [], []
    for it in items:
        if it.get("free"):
            pending_free.append(it)
            continue
        ri = it["ri"]
        y_item = rows[ri][0]["y"]
        # 本条目创建前，处理积压的自由行
        for fr in pending_free:
            text, y = fr["text"], fr["y"]
            mcode = MONTH_CODE.match(text)
            if mcode and y_item - y < 8:   # 名称行上方的孤立月注 → months_detail
                it["md"][mcode.group(1)] = int(mcode.group(2))
                continue
            if NOTE_HINT.search(text):
                if y_item - y < 8:   # 名称行上方的条件文字 → note
                    it["note"] = clean((it.get("note") or "") + text)
                else:
                    leftovers.append({"page": page, "line": text})
                continue
            if not CN_TEXT.search(text):
                leftovers.append({"page": page, "line": text})
                continue
            if not it["name"]:
                it["name"] = text            # 名称行（编号行上方）
                continue
            up = built[-1] if built else None
            y_up = rows[up["ri"]][0]["y"] if up else -1e9
            if y - y_up < y_item - y and up is not None:
                up["name"] = clean(up["name"] + text)   # 折行：距上方条目更近
            elif text in KNOWN_CATEGORIES:
                cat = {"name": text, "items": []}
                state.setdefault("categories", []).append(cat)
            elif up is not None:
                up["name"] = clean(up["name"] + text)
            else:
                leftovers.append({"page": page, "line": text})
        pending_free = []
        it["name"] = clean(it["name"])
        if cat is None:
            cat = {"name": "（未分类）", "items": []}
            state.setdefault("categories", []).append(cat)
        entry = {"no": it["no"], "name": it["name"], "actions": it["actions"]}
        if it["months"] is not None:
            entry["months"] = it["months"]
        if it["note"]:
            entry["note"] = clean(it["note"])
        if it["md"]:
            entry["months_detail"] = it["md"]
        entry["ri"] = ri
        cat["items"].append(entry)
        built.append(it)
    for fr in pending_free:  # 表尾残留
        leftovers.append({"page": page, "line": fr["text"]})
    state["category"] = cat
    for c in state.get("categories", []):
        for e in c["items"]:
            e.pop("ri", None)


def parse_additional(rows, page, add_state, leftovers):
    """附加表按条件分组：A-1/A-2/A-3/B-1 各组内同名项目可重复出现。
    间隔块在名称行上下 ±7pt（三明治结构）；条件行开新组
    （重复条件文本 = 跨页延续同组）。"""
    groups = add_state.setdefault("groups", {})
    started = add_state.get("started", False)
    name_rows, frag_rows, switches = [], [], []
    for row in rows:
        text = rtext(row)
        y = row[0]["y"]
        if not started:
            if text.startswith("附加保养计划"):
                started = True
            continue
        if not text or LEGEND.match(text) or text in ("保养和维护", "附加保养计划") \
                or re.match(r"^\*[:：]", text) or re.search(r"7-2\s*\.?\s*保养\s*\d*$", text):
            continue
        cm = re.match(r"^([AB]-\d+)：", text)
        if cm:
            switches.append((y, cm.group(1)))
            continue
        left = "".join(t["w"] for t in row if t["x"] < 280)
        right = "".join(t["w"] for t in row if t["x"] >= 280)
        if left and re.match(r"^(检查|紧固|更换|添加|调节|清洁)", left):
            name_rows.append({"y": y, "name": left, "right": right})
        elif right:
            frag_rows.append({"y": y, "text": right})
        elif left and name_rows and y - name_rows[-1]["y"] < 14:
            name_rows[-1]["name"] += left   # 名称折行（仅限单元格内 y 邻近）
        elif left:
            leftovers.append({"page": page, "line": left})
    for fr in frag_rows:
        # 片段归属：名称行 y 的中点分界（多行间隔可超出 ±14pt）
        ys = [n["y"] for n in name_rows]
        for i, n in enumerate(name_rows):
            lo = (ys[i - 1] + ys[i]) / 2 if i > 0 else ys[i] - 16
            hi = (ys[i] + ys[i + 1]) / 2 if i + 1 < len(ys) else ys[i] + 20
            if lo < fr["y"] <= hi:
                n.setdefault("frags", []).append(fr)
                break
        else:
            leftovers.append({"page": page, "line": f"间隔片段未配对: {fr['text'][:30]}"})
    # 名称行 → 条件：取该行上方最后一个条件切换（与跨页遗留条件衔接）
    carry = add_state.get("carry_cond")
    for n in name_rows:
        prior = [s for s in switches if s[0] < n["y"]]
        n["cond"] = prior[-1][1] if prior else carry
    if switches:
        add_state["carry_cond"] = switches[-1][1]
    add_state["started"] = started
    for n in name_rows:
        c = n.get("cond") or "无标记"
        groups.setdefault(c, []).append({
            "name": clean(n["name"]),
            "interval": " ".join(t for _, t in sorted(
                [(f["y"], f["text"]) for f in n.get("frags", [])] +
                ([(n["y"], n["right"])] if n["right"] else []))) or "（未识别）"})


def parse_additional_only(rows, page, add_state, leftovers):
    """跨页续表可能没有主表表头，沿用前页条件继续解析附加表。"""
    if add_state.get("started") or any("附加保养计划" in rtext(r) for r in rows):
        parse_additional(rows, page, add_state, leftovers)


def main():
    """按章节定位保养表，逐页解析并保留无法识别的行供复核。"""
    variant = sys.argv[1] if len(sys.argv) > 1 else ""
    if variant not in FILES:
        print("用法: extract_schedule.py gasoline|hybrid")
        return 2
    pdf = REPO / "data" / FILES[variant]
    gdir = WORKDIR / variant
    state, add_state, leftovers = {}, {}, []

    # 保养章范围（页码探测边界）
    outline = json.loads((gdir / "outline.json").read_text(encoding="utf-8"))
    ch_start, ch_end = None, 10**9
    for i, o in enumerate(outline):
        if o["level"] == 0 and "保养和维护" in o["title"]:
            ch_start = o["page"]
            ch_end = next((y["page"] for y in outline[i + 1:] if y["level"] == 0), 10**9)
            break
    if ch_start is None:
        raise SystemExit("outline 中找不到保养章")

    main_pages, add_pages = [], []
    with (gdir / "pages_layout.jsonl").open(encoding="utf-8") as f:
        for rec in f:
            d = json.loads(rec)
            if not (ch_start <= d["page"] <= ch_end):
                continue
            if re.search(r"\b\d{2}(\s+\d{2}){4,}\s*$", d["text"], re.M):
                main_pages.append(d["page"])
            # 表标题独立成行且在主表之后才算（保养须知的同名列表项、
            # 行文提及如“（请参见附加保养计划）”都不算）
            if re.search(r"(?m)^\s*附加保养计划\s*$", d["text"]):
                add_pages += [d["page"], d["page"] + 1]  # 附加表跨页延续
    pages = sorted(set(main_pages + [p for p in add_pages if p > max(main_pages, default=0)]))
    for p in pages:
        parse_page(page_rows(p, pdf), p, state, add_state, leftovers)

    cats = state.get("categories", [])
    add_groups = [{"condition": k, "items": v}
                  for k, v in add_state.get("groups", {}).items()]
    out = gdir / "specs_maintenance_schedule.json"
    items_n = sum(len(c["items"]) for c in cats)
    out.write_text(json.dumps(
        {"table": f"定期保养计划（{variant} 用户手册）",
         "pages": {"main": sorted(set(main_pages)), "additional": sorted(set(add_pages))},
         "code_legend": {"I": "检查，如有必要进行调节或更换",
                         "R": "更换、更改或润滑", "T": "紧固至规定扭矩"},
         "categories": cats, "additional_schedule": add_groups,
         "leftovers": leftovers},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"主表: {len(cats)} 分类 / {items_n} 项; 附加表: {len(add_groups)} 条件组 / "
          f"{sum(len(g['items']) for g in add_groups)} 项; 未识别 {len(leftovers)} 行 → {out}")
    for c in cats:
        nos = [i["no"] for i in c["items"]]
        print(f"  [{c['name']}] {len(c['items'])} 项" + (f" ({min(nos)}-{max(nos)})" if nos else " 空"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

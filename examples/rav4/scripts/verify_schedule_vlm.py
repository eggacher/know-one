"""双通道校验：VLM 盲读定期保养表，与几何解析结果对拍。

通道 A（几何）: extract_schedule.py → specs_maintenance_schedule.json
通道 B（VLM） : qwen3-vl-4b-instruct 盲读页面图片 → 同构 JSON
                  （盲读 = 提示词不含通道 A 的任何结果，避免锚定）

对拍维度（按严重度）:
- CONFLICT  actions / months / 条目存在性 不一致 → 必须人工终审
- MINOR     名称模糊差异(≥0.75) / 分类归属 / note → 建议人工抽查
- AGREE     关键字段一致

输出: workdir/gasoline/verify_report.json + 控制台摘要
原始 VLM 响应留存 vlm_raw_*.json 备审计。
"""
import base64
import difflib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[3]
WORKDIR = Path(__file__).resolve().parents[1] / "workdir"

FILES = {
    "gasoline": "Rav4用户手册（汽油版）.pdf",
    "hybrid": "RAV4HEV用户手册（混动版）.pdf",
}

API = "http://192.168.2.6:1234/v1/chat/completions"
MODEL = "qwen3-vl-4b-instruct"

MAIN_PROMPT = """这是丰田RAV4用户手册「定期保养计划」表的一页。请只依据图片，
把表格逐条目提取为 JSON（不要解释，不要 markdown 代码块）：
{
  "categories": [
    {"name": "分类名，如 发动机基本部件/点火系统/燃油和排放控制系统/底盘和车身",
     "items": [
       {"no": 1, "name": "项目名（折行合并；去掉《参见注释x。》标记，保留项目名本身）",
        "actions": {"20": "I", "40": "I"},
        "months": 24,
        "months_detail": {"I": 6, "R": 24},
        "note": "项目名内嵌的条件文字，如 每行驶100000km（公里）更换一次；没有则 null"}
     ]}
  ]
}
规则：
- actions：键为里程列数字（10/20/30/.../80，单位千公里），值为该列格子中的单字母 I/R/T；
  该列无字母则不写该键。actions 可为空对象 {}。
- months：月数列的数字；若显示为 “–” 写 null。
- months_detail：仅当月数按代码区分（如 I：6 R：24）时填写，否则 null。
- 分类行（独立成行的部件组名）作为 category；若条目上方无分类则归入上一可见分类。
- 条目按 no 编号，严格照图，不要遗漏。"""

ADD_PROMPT = """这是丰田RAV4用户手册「附加保养计划」表的两页（恶劣条件下的保养）。
请只依据图片，把两张表合并提取为 JSON（不要解释）：
{
  "additional": [
    {"name": "项目名（如 检查*制动衬块和制动盘；跨页重复的行只算一条，间隔取完整内容）",
     "interval": "每行驶5000km（公里）或每隔3个月",
     "condition": "A-1：..."}
  ]
}
规则：
- interval 用图中右侧列的完整文字（去掉多余空格）；
- condition 是左侧 A-1/A-2 等条件说明，若属于无条件项则 null；
- 跨页折行的项目把文字接续完整。"""


def render_pages(pdf, img_dir, pages):
    """把目标页渲染为图片；已存在的图片直接复用以减少重复计算。"""
    img_dir.mkdir(parents=True, exist_ok=True)
    for p in pages:
        if not (img_dir / f"p-{p}.png").exists():
            subprocess.run(["pdftoppm", "-r", "150", "-png", "-f", str(p), "-l", str(p),
                            str(pdf), str(img_dir / "p")], check=True)


def b64(img_dir, p: int) -> str:
    return base64.b64encode((img_dir / f"p-{p}.png").read_bytes()).decode()


def ask_vlm(gdir, img_dir, prompt: str, pages: list[int], tag: str) -> dict:
    """盲读指定页面并缓存原始响应；缓存损坏时重新请求。"""
    cached = gdir / f"vlm_raw_{tag}.json"
    if cached.exists():  # 幂等：已完成且可解析的请求直接复用
        try:
            prev = json.loads(cached.read_text(encoding="utf-8"))
            m = re.search(r"\{.*\}", prev.get("content", ""), re.S)
            if m:
                return json.loads(m.group(0))
        except (json.JSONDecodeError, KeyError):
            pass
    content = [{"type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64(img_dir, p)}"}} for p in pages]
    content.append({"type": "text", "text": prompt})
    body = {"model": MODEL, "temperature": 0, "max_tokens": 8192,
            "messages": [{"role": "user", "content": content}]}
    for attempt in range(2):
        t0 = time.time()
        r = None
        for tries in range(3):  # LM Studio JIT 加载期偶发 400，退避重试
            r = requests.post(API, json=body, timeout=900)
            if r.status_code == 200:
                break
            wait = 30 * (tries + 1)
            print(f"  HTTP {r.status_code}，{wait}s 后重试（{tries+1}/3）", flush=True)
            time.sleep(wait)
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        text = msg.get("content") or ""
        (gdir / f"vlm_raw_{tag}.json").write_text(
            json.dumps({"elapsed": round(time.time() - t0, 1),
                        "content": text, "reasoning": msg.get("reasoning_content")},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        body["messages"].append({"role": "assistant", "content": text})
        body["messages"].append({"role": "user", "content": "输出不是合法JSON。请重新只输出完整JSON。"})
    raise RuntimeError(f"VLM 两次未返回合法 JSON: {tag}")


# ---------- 归一化与对拍 ----------

NAME_NOISE = re.compile(r"<<.*?>>|《参见注.*|释\s*\d*。?>>*|\*+")
MONTHS_DASH = ("–", "—", "-")


def norm_name(s):
    return NAME_NOISE.sub("", s or "").replace(" ", "").replace("　", "")


def norm_actions(a):
    out = {}
    for k, v in (a or {}).items():
        try:
            col = str(int(str(k).strip()))
        except ValueError:
            continue
        v = str(v).strip().upper()[:1]
        if v in "IRT":
            out[col] = v
    return out


def norm_months(m):
    if m is None:
        return None
    if isinstance(m, str):
        s = m.strip()
        if s in MONTHS_DASH:
            return "–"
        d = re.sub(r"\D", "", s)
        return int(d) if d else None
    return int(m)


def norm_md(md):
    out = {}
    for k, v in (md or {}).items():
        k, v = str(k).strip().upper()[:1], re.sub(r"\D", "", str(v))
        if k in "IRT" and v:
            out[k] = int(v)
    return out


def name_ratio(a, b):
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def diff_main(geo, vlm):
    """按条目编号对拍主表，保留字段级冲突供人工裁决。"""
    geo_items, vlm_items = {}, {}
    geo_cat, vlm_cat = {}, {}
    for c in geo["categories"]:
        for it in c["items"]:
            geo_items[it["no"]] = it
            geo_cat[it["no"]] = c["name"]
    for c in vlm.get("categories", []):
        for it in c.get("items", []):
            if isinstance(it.get("no"), int):
                vlm_items[it["no"]] = it
                vlm_cat[it["no"]] = c.get("name", "")
    rows = []
    for no in sorted(set(geo_items) | set(vlm_items)):
        g, v = geo_items.get(no), vlm_items.get(no)
        if not g:
            rows.append({"no": no, "verdict": "GEO_ONLY",
                         "vlm": {"name": v.get("name"), "actions": norm_actions(v.get("actions"))}})
            continue
        if not v:
            rows.append({"no": no, "verdict": "VLM_ONLY",
                         "geo": {"name": g.get("name"), "actions": norm_actions(g.get("actions"))}})
            continue
        row = {"no": no, "geo": {"name": norm_name(g.get("name")),
                                 "actions": norm_actions(g.get("actions")),
                                 "months": norm_months(g.get("months"))},
               "vlm": {"name": norm_name(v.get("name")),
                       "actions": norm_actions(v.get("actions")),
                       "months": norm_months(v.get("months"))}}
        verdicts = ["AGREE"]
        if row["geo"]["actions"] != row["vlm"]["actions"]:
            verdicts = ["CONFLICT:actions"]
        if row["geo"]["months"] != row["vlm"]["months"]:
            verdicts.append("CONFLICT:months")
        gmd, vmd = norm_md(g.get("months_detail")), norm_md(v.get("months_detail"))
        if gmd != vmd:
            verdicts.append("MINOR:months_detail")
        r = name_ratio(g.get("name"), v.get("name"))
        if r < 0.75:
            verdicts.insert(0, "CONFLICT:name")
        elif r < 1.0:
            verdicts.append("MINOR:name")
        if geo_cat[no] != vlm_cat.get(no, ""):
            verdicts.append("MINOR:category")
        if "CONFLICT:name" in verdicts or r >= 0.75:
            pass
        row["name_ratio"] = round(r, 2)
        row["verdict"] = "AGREE" if verdicts == ["AGREE"] else " ".join(
            v for v in verdicts if not v.startswith("AGREE")) or "AGREE"
        rows.append(row)
    return rows


def diff_additional(geo, vlm):
    """geo 附加表为条件分组结构；VLM 为平铺（含 condition 字段）。
    匹配键 = (条件前缀, 名称模糊)，同名项目跨条件重复时按双键配对。"""
    geo_flat = {}  # 条件和名称共同定位条目；同名项目可在不同条件组重复。
    for grp in geo.get("additional_schedule", []):
        cond = re.match(r"([AB]-\d+)", grp["condition"] or "")
        cond = cond.group(1) if cond else (grp["condition"] or "?")
        for it in grp["items"]:
            geo_flat[(cond, norm_name(it["name"]))] = it["interval"]
    vlm_add = vlm.get("additional") or []

    def vlm_cond(v):
        m = re.match(r"([AB]-\d+)", v.get("condition") or "")
        return m.group(1) if m else "?"

    rows, used = [], set()
    for v in vlm_add:
        key = norm_name(v.get("name", ""))
        if not key:
            continue
        best, br = None, 0.0
        for (cond, gk), it in geo_flat.items():
            r = difflib.SequenceMatcher(None, key, gk).ratio()
            if vlm_cond(v) == cond:
                r += 0.15  # 条件一致加分，避免 空调/空气滤清器 跨条件错配
            if r > br:
                best, br = (cond, gk), r
        gi = re.sub(r"\s", "", best and geo_flat[best] or "")
        vi = re.sub(r"\s", "", v.get("interval", "") or "")
        if best:
            used.add(best)
        rows.append({"name": f"[{vlm_cond(v)}] {v.get('name')}",
                     "geo_interval": gi[:60] or None,
                     "vlm_interval": vi[:60],
                     "verdict": "AGREE" if gi and gi == vi else "CONFLICT:interval"})
    for (cond, gk), it in geo_flat.items():
        if (cond, gk) not in used:
            rows.append({"name": f"[{cond}] {gk}", "geo_interval": it["interval"][:60],
                         "vlm_interval": None, "verdict": "GEO_ONLY"})
    return rows


def main():
    variant = sys.argv[1] if len(sys.argv) > 1 else ""
    if variant not in FILES:
        print("用法: verify_schedule_vlm.py gasoline|hybrid")
        return 2
    pdf = REPO / "data" / FILES[variant]
    gdir = WORKDIR / variant
    img_dir = gdir / "pages_img"
    geo = json.loads((gdir / "specs_maintenance_schedule.json").read_text(encoding="utf-8"))
    meta = geo["pages"]
    render_pages(pdf, img_dir, sorted(set(meta["main"] + meta["additional"])))

    vlm = {"categories": [], "additional": []}
    for p in meta["main"]:
        print(f"VLM 盲读主表 p{p} ...", flush=True)
        vlm["categories"] += ask_vlm(gdir, img_dir, MAIN_PROMPT, [p], f"main_p{p}").get("categories", [])
    add_pages = [p for p in meta["additional"] if p not in meta["main"]]
    if add_pages:
        print(f"VLM 盲读附加表 p{'+p'.join(map(str, add_pages))} ...", flush=True)
        vlm["additional"] = ask_vlm(gdir, img_dir, ADD_PROMPT, add_pages,
                                    "add_p" + "_".join(map(str, add_pages))).get("additional", [])

    (gdir / "vlm_schedule.json").write_text(
        json.dumps(vlm, ensure_ascii=False, indent=1), encoding="utf-8")

    main_rows = diff_main(geo, vlm)
    add_rows = diff_additional(geo, vlm)

    conflict = [r for r in main_rows
                if r["verdict"].startswith("CONFLICT") or r["verdict"] in ("GEO_ONLY", "VLM_ONLY")]
    minor = [r for r in main_rows if "MINOR" in r["verdict"] and r not in conflict]
    agree = [r for r in main_rows if r["verdict"] == "AGREE"]

    report = {"variant": variant, "main": main_rows, "additional": add_rows,
              "summary": {"main_total": len(main_rows), "agree": len(agree),
                          "minor": len(minor), "conflict": len(conflict),
                          "add_total": len(add_rows),
                          "add_conflict": sum(1 for r in add_rows if r["verdict"] != "AGREE")}}
    (gdir / "verify_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    s = report["summary"]
    print(f"\n主表对拍: {s['main_total']} 项 = AGREE {s['agree']} / MINOR {s['minor']} / CONFLICT+缺失 {s['conflict']}")
    print(f"附加表对拍: {s['add_total']} 项, 冲突 {s['add_conflict']}")
    print("\n── 需人工终审 ──")
    for r in conflict:
        g, v = r.get("geo"), r.get("vlm")
        print(f"  #{r['no']:>2} {r['verdict']}")
        if g and v:
            print(f"      geo: {g['name'][:18]:<20} {g['actions']} 月:{g['months']}")
            print(f"      vlm: {v['name'][:18]:<20} {v['actions']} 月:{v['months']}")
        elif g:
            print(f"      geo: {g['name'][:24]} {g['actions']}（VLM 漏读）")
        else:
            print(f"      vlm: {v['name'][:24]} {v['actions']}（几何通道漏了）")
    for r in add_rows:
        if r["verdict"] != "AGREE":
            print(f"  附加[{r['verdict']}] {r['name'][:16]}")
            print(f"      geo: {r.get('geo_interval')}\n      vlm: {r.get('vlm_interval')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

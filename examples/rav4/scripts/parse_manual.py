"""数字版手册解析：书签 → outline.json，逐页文本 → pages.jsonl。

用法: python parse_manual.py gasoline|hybrid
仅适用于文本层完好的 PDF；扫描版走 VLM 路线（后续脚本）。
"""
import json
import re
import subprocess
import sys
from pathlib import Path

from pypdf import PdfReader

REPO = Path(__file__).resolve().parents[3]
DATA = REPO / "data"
WORKDIR = Path(__file__).resolve().parents[1] / "workdir"

FILES = {
    "gasoline": "Rav4用户手册（汽油版）.pdf",
    "hybrid": "RAV4HEV用户手册（混动版）.pdf",
}


def extract_outline(reader: PdfReader) -> list[dict]:
    """书签树 → 扁平列表；page 统一为 1 起始的 PDF 页码。"""
    out = []

    def walk(items, level=0):
        for it in items or []:
            if isinstance(it, list):
                walk(it, level + 1)
            else:
                try:
                    page0 = reader.get_destination_page_number(it)
                except Exception:
                    continue  # 跳过不可解析的书签
                out.append({"level": level, "title": str(it.title), "page": page0 + 1})

    walk(reader.outline)
    return out


def chap_names_from_toc(pages_layout: dict[int, str], max_page: int = 6) -> dict[int, str]:
    """目录页（前几页）扫描“N 章名”短行，返回 {章号: 章名}。多栏交错可能漏个别章。"""
    names: dict[int, str] = {}
    pat = re.compile(r"^(\d{1,2})\s+([\u4e00-\u9fff][\u4e00-\u9fff（）a-zA-Z0-9·]{1,14})$")
    for page in sorted(pages_layout):
        if page > max_page:
            break
        for ln in pages_layout[page].splitlines():
            m = pat.match(ln.strip())
            if m and int(m.group(1)) not in names:
                names[int(m.group(1))] = m.group(2).strip()
    return names


def derive_outline_from_heads(pages_layout: dict[int, str], reader: PdfReader) -> list[dict]:
    """无书签 PDF 的回退方案：页眉推小节边界；章名优先取目录，
    兜底用该章第一个节名。页眉标示“该页当前所属”小节，
    边界可能比实际晚半页，误差可接受。"""
    sect_re = re.compile(r"(?<!\d)(\d{1,2})-(\d{1,2})\.?\s*([^\s\d][^\n]*)")
    bounds, cur = [], None
    for page in sorted(pages_layout):
        head = "\n".join(pages_layout[page].splitlines()[:2])
        m = sect_re.search(head)
        if not m:
            continue
        key = f"{m.group(1)}-{m.group(2)}"
        if key == cur:
            continue
        bounds.append({"page": page, "chap": int(m.group(1)), "sect": m.group(2),
                       "title": m.group(3).rstrip("0123456789 ").strip()})
        cur = key

    out: list[dict] = []
    toc = chap_names_from_toc(pages_layout)
    cur_chap = None
    for bi, b in enumerate(bounds):
        if b["chap"] != cur_chap:
            first_sect = next((x["title"] for x in bounds if x["chap"] == b["chap"]), "")
            name = toc.get(b["chap"]) or first_sect or "未名章节"
            out.append({"level": 0, "title": f"{b['chap']}.{name}", "page": b["page"]})
            cur_chap = b["chap"]
        out.append({"level": 1, "title": f"{b['chap']}-{b['sect']}.{b['title']}",
                    "page": b["page"]})
    return out


def chap_name(reader: PdfReader, b: dict) -> str:
    """pypdf 页眉顺序固定：页码/章号/小节行/章名 → 章号行的下两行。"""
    try:
        text = reader.pages[b["page"] - 1].extract_text() or ""
    except Exception:
        return "未名章节"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    sect_prefix = f"{b['chap']}-{b['sect']}"
    for i, ln in enumerate(lines):
        if ln == str(b["chap"]) and i + 2 < len(lines) \
                and lines[i + 1].startswith(sect_prefix):
            cand = lines[i + 2]
            if not sect_re.match(cand) and re.search(r"[\u4e00-\u9fff]", cand):
                return cand[:20]
    return "未名章节"


def main() -> int:
    """同时保存阅读顺序文本与版面文本，供切块和表格解析分别使用。"""
    key = sys.argv[1] if len(sys.argv) > 1 else ""
    if key not in FILES:
        print("用法: parse_manual.py gasoline|hybrid")
        return 2

    pdf = DATA / FILES[key]
    outdir = WORKDIR / key
    outdir.mkdir(parents=True, exist_ok=True)

    reader = PdfReader(pdf)

    # 两种模式各取所长：
    #   默认模式：按内容流顺序，双栏排版阅读序正确 → 适合正文切块
    #   -layout：保留版面列位 → 适合表格列对齐解析
    texts = {}
    for suffix, args in (("pages.jsonl", []), ("pages_layout.jsonl", ["-layout"])):
        text = subprocess.run(
            ["pdftotext", *args, "-enc", "UTF-8", str(pdf), "-"],
            capture_output=True, text=True, check=True,
        ).stdout
        texts[suffix] = text

    outline = extract_outline(reader)
    src = "书签"
    if not outline:
        # 无书签（如混动版）：layout 页眉推导章节骨架
        pages_layout = {i: p for i, p in enumerate(texts["pages_layout.jsonl"].split("\f"),
                                                   start=1)
                        if i <= len(reader.pages)}
        outline = derive_outline_from_heads(pages_layout, reader)
        src = "页眉推导"
    (outdir / "outline.json").write_text(
        json.dumps(outline, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"outline: {len(outline)} 条（来源: {src}）")

    for suffix in ("pages.jsonl", "pages_layout.jsonl"):
        pages = texts[suffix].split("\f")
        with (outdir / suffix).open("w", encoding="utf-8") as f:
            for i, page in enumerate(pages, start=1):
                if i > len(reader.pages):
                    break  # 尾部多出的空段
                f.write(json.dumps({"page": i, "text": page}, ensure_ascii=False) + "\n")
    print(f"pages: {len(reader.pages)} 页 → pages.jsonl + pages_layout.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())

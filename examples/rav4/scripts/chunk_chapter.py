"""保养章示范切块（gasoline=第 7 章，hybrid=第 6 章）：小节 → 语义块。

用法: python chunk_chapter.py gasoline|hybrid

输入: pages.jsonl（pdftotext 默认模式，阅读流顺序）
规则（对应 docs/chunking.md 的结构优先原则）：
- 章节边界：outline 中标题含“保养和维护”的章（章节号随变体而定）；
- 页眉预处理：页首块（小节行/侧栏“注意”标签/页码）剥离——
  侧栏标签不再误触发警告块（混动版实测暴露的问题）；
- 块边界：●/•/■ 列表项、编号步骤、警告/注意框、长度上限；
- 默认模式的空行是双栏排版的栏分隔伪影，不作为段落边界；
- ⚠ 警告/注意块整体保留（上限 900 字），绝不截断；
- 普通块上限 600 字。

已知局限：侧栏“注意”框的边界靠启发式，跨栏内容可能混入；
后续可用 VLM 按版面语义复核。

输出: workdir/{variant}/chunks.jsonl
每条: {chunk_id, section_path, page_start, page_end, text, block_type, meta}
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

WORKDIR = Path(__file__).resolve().parents[1] / "workdir"

ID_PREFIX = {"gasoline": "gas", "hybrid": "hev"}

MAX_CHARS = 600
WARN_MAX_CHARS = 900

PAGE_NUM_ONLY = re.compile(r"^\s*\d{1,3}\s*$")
SECTION = re.compile(r"^\s*(\d{1,2}-\d{1,2})\.?\s*(.*)$")
BULLET = re.compile(r"^\s*[•●■]")
NUMBERED = re.compile(r"^\s*(\d{1,2})\s+\S")
WARN_HEAD = re.compile(r"^\s*(警告|注意|提示)\s*$")

SUB_TITLES = {"保养须知", "定期保养", "自行保养注意事项"}


def maintenance_chapter(outline: list[dict]) -> tuple[str, int, int]:
    """outline 中标题含“保养和维护”的章 → (标题, 起始页, 末页)。"""
    for i, o in enumerate(outline):
        if o["level"] == 0 and "保养和维护" in o["title"]:
            end = next((y["page"] for y in outline[i + 1:] if y["level"] == 0), 10**9)
            return o["title"], o["page"], end
    raise SystemExit("outline 中找不到保养章")


def split_page_header(lines: list[str]) -> tuple[str | None, list[str]]:
    """剥离页首块：小节运行头 + 侧栏标签（注意/警告）+ 页码行。

    返回 (页眉中的小节 id 或 None, 正文行)。
    页首块 = 到第一个「纯页码行」为止（最多扫前 6 行）。
    这样可避免页眉里的“注意”被误当成正文警告块。
    """
    header_sect = None
    for i, ln in enumerate(lines[:6]):
        s = ln.strip()
        if PAGE_NUM_ONLY.match(ln):
            return header_sect, lines[i + 1:]
        m = SECTION.match(ln)
        if m and not header_sect:
            header_sect = m.group(1)
        # 侧栏标签（注意/警告/提示）在此丢弃
    return header_sect, lines


def main() -> int:
    variant = sys.argv[1] if len(sys.argv) > 1 else ""
    if variant not in ID_PREFIX:
        print("用法: chunk_chapter.py gasoline|hybrid")
        return 2
    vdir = WORKDIR / variant
    outline = json.loads((vdir / "outline.json").read_text(encoding="utf-8"))
    chap_title, start, end = maintenance_chapter(outline)
    chap_no = chap_title.split(".")[0]
    section_re = re.compile(rf"^\s*({chap_no}-\d+)\.?\s*(.*)$")
    chap_path = chap_title.replace(" ", "")

    lines: list[tuple[int, str]] = []
    with (vdir / "pages.jsonl").open(encoding="utf-8") as f:
        for rec in f:
            d = json.loads(rec)
            if not (start <= d["page"] < end):
                continue
            body_lines = d["text"].splitlines()
            # 首页：章扉页无页码头，直接用全文
            header_sect, body = split_page_header(body_lines)
            if header_sect:
                ln = next(l for l in body_lines if SECTION.match(l.strip()))
                m = section_re.match(ln.strip()) or SECTION.match(ln.strip())
                lines.append((d["page"], f"{m.group(1)}. {m.group(2).strip()}"))
            for ln in body:
                s = ln.strip()
                if s and s != "保养和维护" and not PAGE_NUM_ONLY.match(ln):
                    lines.append((d["page"], ln))

    chunks: list[dict] = []
    section = chap_path
    buf: list[str] = []
    buf_page: int | None = None
    page = start
    block_type = "paragraph"

    def flush():
        """把当前缓冲区封成一个块，并保留起止页供后续定位。"""
        nonlocal buf, buf_page, block_type
        text = "".join(buf).strip()
        if text:
            path = chap_path if section == chap_path else f"{chap_path} > {section}"
            chunks.append({
                "chunk_id": f"{ID_PREFIX[variant]}-ch{chap_no}-{len(chunks)+1:03d}",
                "section_path": path,
                "page_start": buf_page, "page_end": page,
                "text": text, "block_type": block_type,
                "meta": {"doc_type": "owner_manual", "variant": variant},
            })
        buf, buf_page = [], None

    for page, ln in lines:
        s = ln.strip()
        if not s:
            continue  # 栏分隔伪影
        m = section_re.match(s)
        if m:
            flush()
            section = f"{m.group(1)}.{m.group(2).strip()}" if m.group(2) else m.group(1)
            block_type = "paragraph"
            continue
        if s in SUB_TITLES:
            flush()
            section = s
            block_type = "paragraph"
            continue
        if WARN_HEAD.match(s):
            # 正文里的独立警告标题开启新块，避免与上一段普通说明混在一起。
            flush()
            block_type = "warning"
            buf_page, buf = page, [s]
            continue
        if block_type == "warning":
            # 警告块按较宽的上限整体保留；超限时结束当前警告块。
            buf.append(s)
            if sum(map(len, buf)) > WARN_MAX_CHARS:
                flush()
                block_type = "paragraph"
            continue
        if BULLET.match(s):
            flush()
            block_type = "bullet"
            buf_page, buf = page, [s]
            continue
        if NUMBERED.match(s) and len(s) > 3:
            flush()
            block_type = "step"
            buf_page, buf = page, [s]
            continue
        if buf_page is None:
            buf_page = page
        buf.append(s)
        if sum(map(len, buf)) > MAX_CHARS:
            flush()
            block_type = "paragraph"
    flush()

    out = vdir / "chunks.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    stat = Counter(c["block_type"] for c in chunks)
    print(f"{chap_title}: PDF p{start}-{end-1}, {len(chunks)} 个 chunk → {out}")
    print(f"块类型分布: {dict(stat)}")
    for s_ in sorted({c['section_path'] for c in chunks}):
        print(f"  - {s_}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

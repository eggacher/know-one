"""语料登记：计算内容哈希与页数，写入 workdir/sources.json。

对应 KnowOne 的 Source 概念：原始文件不可变，以 sha256 为身份。
"""
import hashlib
import json
import sys
from pathlib import Path

from pypdf import PdfReader

REPO = Path(__file__).resolve().parents[3]
DATA = REPO / "data"
WORKDIR = Path(__file__).resolve().parents[1] / "workdir"

DOCS = [
    # (文件, doc_type, variant, 文本层状态)
    ("Rav4用户手册（汽油版）.pdf", "owner_manual", "gasoline", "ok"),
    ("RAV4HEV用户手册（混动版）.pdf", "owner_manual", "hybrid", "ok"),
    ("RAV4多媒体用户手册.pdf", "multimedia", "common", "scan"),
    ("RAV保养手册.pdf", "maintenance", "common", "vector"),
]


def sha256(path: Path) -> str:
    """分块计算文件指纹，避免一次把整本 PDF 读入内存。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> None:
    WORKDIR.mkdir(parents=True, exist_ok=True)
    out = []
    for filename, doc_type, variant, text_layer in DOCS:
        path = DATA / filename
        digest = sha256(path)
        reader = PdfReader(path)
        out.append(
            {
                "source_id": digest[:16],
                "file": filename,
                "sha256": digest,
                "size_bytes": path.stat().st_size,
                "pages": len(reader.pages),
                "doc_type": doc_type,
                "variant": variant,
                "text_layer": text_layer,
            }
        )
        print(f"{digest[:16]}  {doc_type:14s} {variant:8s} {len(reader.pages):4d}页  {filename}")
    (WORKDIR / "sources.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n→ {WORKDIR / 'sources.json'}")


if __name__ == "__main__":
    sys.exit(main())

# RAV4 2019 手册入库管线（练手项目）

用 KnowOne 的数据模型对四本丰田 RAV4 手册做完整入库：解析、切块、
结构化抽取、元数据标注、向量化（LM Studio 本地模型）。

## 语料登记（workdir/sources.json）

| source_id | 文件 | doc_type | variant | 页数 | 处理方式 |
|---|---|---|---|---|---|
| sha256 前缀 | `data/Rav4用户手册（汽油版）.pdf` | owner_manual | gasoline | 432 | pdftotext（文本层完好） |
| | `data/RAV4HEV用户手册（混动版）.pdf` | owner_manual | hybrid | 404 | pdftotext（文本层完好） |
| | `data/RAV4多媒体用户手册.pdf` | multimedia | common | 57 | qwen3-vl-4b OCR（纯扫描） |
| | `data/RAV保养手册.pdf` | maintenance | common | 65 | qwen3-vl-4b 表格还原 + 双通道校验 |

变体（variant）是 chunk 级元数据：多媒体/保养手册按内容混标 common，
用户手册整本 gasoline/hybrid、混动专属章节额外标 hybrid-only。

## 目录

```
scripts/
  register_sources.py    # 计算 sha256、页数，登记 sources.json
  parse_manual.py        # 书签→outline.json；pdftotext→pages.jsonl（双模式）
  chunk_chapter.py       # 第 7 章示范切块：小节→语义块
  extract_schedule.py    # 定期保养表→结构化 JSON（bbox 词级坐标几何解析）
  verify_schedule_vlm.py # VLM 盲读对拍（qwen3-vl-4b，响应缓存幂等）
  adjudicate_schedule.py # 双通道终审合并 → final.json + 裁决记录
workdir/                 # 产物，不入库（.gitignore）
  gasoline/specs_maintenance_schedule.json        # 几何通道
  gasoline/vlm_schedule.json + vlm_raw_*.json     # VLM 通道（原始响应留审计）
  gasoline/specs_maintenance_schedule.final.json  # 终审权威版
  gasoline/adjudication_notes.md                  # 裁决记录
```

## 依赖与模型

- pdftotext（poppler）、pypdf
- LM Studio `http://localhost:1234`（也可通过脚本参数改为实际地址）：
  embedding 用 `text-embedding-qwen3-embedding-0.6b`，
  扫描/表格用 `qwen3-vl-4b-instruct`，文本推理用 `qwen3.5-9b`

## 进度

- [x] 汽油版解析 + 第 7 章切块（348 块，页眉预处理后重切）+ 保养表终审
- [x] 混动版：无书签 → 页眉推导章节骨架（保养章为第 6 章 p274-327）；
      切块 355 块；保养表主表 26 项（含 1 条仅 VLM 读出、整条补入的
      “气门机构”）+ 附加表 5 条件组 23 项；待复查 0 处；
      混动特有条目：动力控制单元冷却液、12伏蓄电池、混合动力蓄电池
      冷却进气滤清器、后差速器油（集成在后传动桥中）
- [x] 双通道校验模式已参数化（extract/verify/adjudicate 均 gasoline|hybrid），
      保养手册表格还原直接复用
- [ ] 多媒体手册 OCR
- [ ] 保养手册表格还原（复用双通道模式）
- [ ] 全量向量化 + SQLite 检索

KnowOne 的全量检索示例不复用上述 SQLite 计划。对于有文本层的两本用户手册，先创建车型专属 Namespace，再运行：

```bash
.venv/bin/python -m know_one create-namespace rav4-gasoline
PYTHONPATH=. .venv/bin/python examples/rav4/scripts/ingest_owner_manual.py \
  --variant gasoline --namespace rav4-gasoline --principal evaluator
```

混动版使用 `rav4-hybrid` Namespace 与 `--variant hybrid`。当前核心 PDF 管线不应直接导入扫描型多媒体手册或以表格为主的保养手册；它们分别需要 OCR 与结构化表格解析后再评估。

`eval/hybrid_smoke.jsonl` 是混动版的 22 条开发冒烟样本，页码和原文片段均从混动手册独立核对，不能复用汽油版 `smoke.jsonl`。同一安全操作在多页有等价表述时，样本列出全部已核对的可接受页码，避免把正确的替代原文误判为漏检。它用于快速发现版本间页码、措辞和排序差异；在独立人工复核后才可作为验收 Golden Set。发布混动手册后可运行：

```bash
PYTHONPATH=. .venv/bin/python -m know_one.eval.smoke \
  --dataset examples/rav4/eval/hybrid_smoke.jsonl \
  --namespace rav4-hybrid --principal evaluator --deadline-ms 15000 \
  --include-miss-evidence
```

## 双通道校验经验（首战记录）

1. **几何通道强在「格」**：pdftotext -bbox 的词级坐标与列锚点实测
   距离 Δ0.0，I/R/T→万公里列分配无可争议；弱在「行装配」：
   名称行/折行/分类行三态歧义多。
2. **VLM（qwen3-vl-4b）强在「行」弱在「列」**：名称全部读全，但列位
   系统性错位（把 20/40/60/80 读成 10/30/50/70）、无规律漏读。
3. 裁决 = 各取所长：actions/months 取几何，name/note 取 VLM。
4. 附加保养计划表是**按条件分组**的表（A-1/A-2/A-3/B-1），
   同名项目跨条件重复（如“紧固传动轴螺栓”出现 4 次、间隔不同）——
   平铺解析必错，结构必须建模为 {condition: [items]}。
5. VLM 一次全页表格盲读约 5-6 分钟（本地 4B），提示词严禁携带另一通道
   结果（防锚定）；原始响应必须留存审计。
6. months 与 months_detail 是同一信息的等价编码（月数列写 R：24 时
   通道 A 记 md、通道 B 记 months），对拍前先归一，否则全是假冲突。
7. 无书签 PDF 的骨架来源优先级：页眉小节号定边界 > 目录页补章名 >
   章首个节名兜底。注意：页眉小节标题与保养须知里的同名列表项、
   行文提及都要区分（用「独立成行 + 在主表之后」约束）。
8. 混动版与汽油版章节编号不同（保养章分别是 6/7 章），
   脚本一律按「标题含保养和维护」定位，不硬编码章号。

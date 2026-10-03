# 接口契约

状态：M1 核心接口已实现并有 PostgreSQL 集成测试：来源快照入库、IndexGeneration、发布、撤回、ACL、删除和受约束检索均可运行。`retrieve(include_context=True)` 可为主 Evidence 附加带独立定位的相邻原文，但不改变主 Evidence 排序。本文同时保留商用首版的目标约束；预览、取消计划发布、长生命周期 Scope 校验和阶段耗时等未在当前代码实现，不能据此当作现有 API 承诺。以 `KnowOne` 的公开签名、测试和 README 运行示例为当前可调用行为的依据。

## 共同约束

所有接口接收由宿主后端构造的可信 AccessScope，至少包含 principal_id、允许的 Namespace、操作权限和文档可见范围／用户组。浏览器或终端用户不得直接提交自声明的 scope；库不承担用户认证，但必须执行授权条件。scope 过期或无法验证时失败关闭。

用户离组、停用等身份变化由宿主保证 scope 的新鲜度；长生命周期 scope 必须携带可核验的授权版本，不能仅依赖过期时间延迟撤权。Document ACL 由库按当前状态执行。该信任约定针对宿主后端接入，不提供对可任意执行 Python 的恶意调用进程的隔离。

read、ingest、publish、withdraw、manage_acl、delete 分别授权。未知或无权访问的文档对外统一为 NotFoundOrForbidden，避免暴露存在性。管理状态和预览同样检查权限。首版不支持文档内部不同段落具有不同 ACL；这类内容必须拆成不同 Document 或拒绝入库。

所有时间使用带时区时间，内部统一 UTC，拒绝 naive datetime。Validity Window 使用 [valid_from, valid_until)，null 上界表示无限；必须满足下界小于上界。省略 at 时每次请求只捕获一次当前时间。

请求应携带 trace_id 或由库生成；参数有明确长度、数量和 deadline 上限，部署配置中的上限必须经过验收。SQL 参数化，模型和解析器输入不能直接形成数据库指令。

## 文档身份、业务时间与权限

Document 身份为 (namespace, source_key)。source_key 来自稳定外部 ID 或宿主明确分配的标识，不使用可变化的标题或内容 hash。一个 Document 固定一份业务适用范围；修改范围需新建身份并明确撤回旧文档。

DocumentRevision 的内容不可变；Publication 定义哪个 revision 在什么业务时间生效。内容更新不删除旧 revision。正常检索必须同时满足：

- Document 属于请求 Namespace，当前未撤回／删除，且当前 ACL 允许访问。
- 产品、地区、渠道等适用条件匹配；文档声明适用条件但请求缺少必要条件时返回 InvalidArgument，不推测或跨范围放宽。
- at 落在有效 Publication 区间内。
- revision 在本次捕获的 IndexGeneration 中构建完整。

at 表示“按现在发布记录查询某个业务时刻适用的内容”，不支持重放“当时系统已知什么”。历史查询仍执行当前 ACL；撤回立即作用于所有 at。首版限制 at 不晚于当前时刻，未来公告只能通过具备管理权限的预览查看，防止提前泄露。

## ingest 与任务状态

ingest(source, namespace, source_key, access_scope, idempotency_key) 返回持久化后的 IngestionJobRef，不承诺调用返回时可检索。source 必须能够被 worker 持久读取；提交时保存不可变快照或版本化引用，不能只留下临时文件路径。新增 Document 默认仅编辑者可见，业务读取 ACL 由管理接口明确配置。

幂等键在 (namespace, operation, idempotency_key) 内唯一，并作为 `operation_receipt` 持久化。请求指纹包含来源版本／内容摘要、source_key 和构建参数；同键同指纹返回首次操作的任务或结果，同键不同指纹返回 IdempotencyConflict。状态变更操作的成功回执与状态、审计在同一事务中提交；关键操作的幂等记录随审计保留，清理后不再承诺无限期重放幂等。

content_hash 仅用于判断是否需要重建内容，且只在同 Document 内考虑复用，不能跨权限范围合并身份。内容未变且当前 IndexGeneration 已有完整构建时，ingest 可直接返回该 revision；内容未变但当前代缺少完整构建时，不新增 revision，只为该 revision 重建索引数据。相同内容但 IndexGeneration 变化仍需构建；ACL 和发布时间修改走管理接口，即使内容不变也执行。

状态：queued → running → ready；异常到 failed 或 needs_review，取消到 cancelled。running 持有可过期 lease，worker 定期续租；重试可能重复执行阶段，写入依靠唯一键和完成标记保证幂等。ready 仅表示校验通过，尚未发布；发布状态另查 Publication。

get_ingestion(job_id, access_scope) 返回阶段、完成计数、错误码、尝试次数、warnings、revision_id 和可预览来源。批量导入返回逐项任务，不用一个“成功”掩盖部分失败。

## publish、withdraw、ACL 与删除

publish(revision_id, namespace, valid_from, valid_until, expected_generation, access_scope, idempotency_key) 仅接受 ready 且其 `(revision_id, current_index_generation_id)` 构建状态为 `ready` 的 revision。expected_generation 是 Document `state_generation`（当前可检索状态的并发控制版本），不是 IndexGeneration。

首版发布默认采用 append/replace-tail 语义：存在当前无上界 Publication 时，新窗口开始点必须晚于旧窗口开始点，事务内把旧窗口截止时间改到新起点并插入新记录；存在其他冲突窗口、乱序回填或未来待发布记录时返回 PublicationConflict，要求运营明确处理，不暗中裁剪多段历史。首次发布可设置过去的起点；后续只能追加非重叠窗口或截短最后一个无上界窗口，不能改写已经封闭的历史窗口。尾部追溯生效会改变历史 at 的查询结果，需在预览中显示影响范围并审计。

在短事务中锁定发布协调对象与 Document，检查 expected_generation、写入窗口和审计、递增 `state_generation`；数据库层约束同一 Document 的有效窗口不重叠，且 Publication 的 revision 必须属于该 Document。外部模型调用不在该事务中。同幂等键重放先返回原结果，不因 generation 已变而误报冲突。

示例：v1 从 9 月 1 日生效，v2 从 9 月 23 日生效。发布 v2 后，at=9 月 10 日返回 v1，当前查询返回 v2。构建 v2 失败不会影响 v1。新窗口结束后不自动恢复 v1；需要显式重新发布。

withdraw(document_id, expected_generation, access_scope, idempotency_key) 更新当前撤回标记、递增 `state_generation` 并记录审计，不删历史。恢复必须是明确管理操作。错误内容的回滚可将旧内容 revision 从当前时刻重新发布，不能覆盖旧原文或伪造过去记录；待生效的发布可通过具备发布权限的取消操作撤销，恢复被它截短的前序窗口须同事务校验无重叠并审计。取消计划发布是前述封闭窗口不可改写规则的受控例外，仅能恢复尚未到达的未来边界。

set_access(document_id, acl, expected_generation, ...) 原子更新当前权限并递增 `state_generation`；回滚内容或索引不回滚权限。撤回和撤权提交后启动的新请求必须使用新状态；已在途请求在返回前重新检查，仍存在检查与发送间的竞态，不承诺撤回已发送数据。需要更严格即时撤权的部署必须在宿主出口增加协调机制。

delete(document_id, ...) 先使内容不可检索，再后台清理 Chunk、向量、原文和缓存；共享原文引用需引用计数检查。保留最小、不含正文的删除审计，状态能够查询。物理清理与备份过期时间由部署策略约定；恢复备份后需重放删除记录，完成前不开放查询。

## retrieve 与 Evidence

retrieve(query, namespace, access_scope, at=None, applicability=..., top_k=8, deadline_ms=..., recall_mode="hybrid") 返回 RetrievalResult。top_k 是最多返回条数，不保证凑满；首版建议允许 1–20，具体上限作为部署配置验证。`recall_mode` 默认为 `hybrid`；`full_text` 和 `vector` 只用于受控诊断和评估，仍执行全部权限、发布、时效及适用条件过滤。

结果字段：

| 字段 | 语义 |
|---|---|
| evidence[] | 主原文片段及定位、必要补充片段及各自定位、document_id、revision_id、chunk_id、Publication 时间窗、标题路径 |
| rank_score / score_type | 当前排序分数及类型，例如 reranker 或 rrf；不当作正确概率 |
| index_generation / model_version | 本次使用的索引和排序模型版本 |
| degraded / warnings | 路径缺失、截断、扫描预算耗尽等已知限制；任一证据缺少关键上下文时包含 `evidence_incomplete` |
| trace_id / stage_timings | 诊断标识和分阶段耗时；详细 query/候选跟踪仅授权调试可见 |

主片段的 `source_locator` 至少包含不可变原文引用和原文区间；格式支持时附章节、页码、表格行号。补充的标题、表头或相邻句如来自其他区间，放在 `context_parts[]` 中，每项都有自己的 `text` 和 `source_locator`；不能用一个区间表示拼接后的不连续原文。来源 URL 可能变动，不能独自承担证据定位。原文下载接口同样鉴权，不把存储凭证放入结果。

空结果表示本次检索没有找到依据；非空结果不保证足以回答。首版不输出未经校准的 answerable=true 或 confidence。最终回答、引用支持检查及拒答策略由宿主实现并一起验收。

每次检索固定一个 IndexGeneration，query 向量与文档向量来自同一模型版本及预处理配置。多路结果回源时校验 `state_generation`；若期间变化，预算内重试一次，否则返回明确并发变化错误，避免组合新旧政策。权限和撤回在返回前再次检查，失效候选剔除；无法检查则失败关闭。

## 接口字段速查

本节只回答「每个字段是什么」；语义细节以上文各节为准。

### ingest

| 参数 | 说明 |
|---|---|
| source | 来源（文件、API 导出、工单记录等）；必须能被 worker 持久读取，提交时保存不可变快照或版本化引用，不接受临时文件路径 |
| namespace | 目标命名空间 |
| source_key | 稳定外部 ID 或宿主分配的标识；(namespace, source_key) 构成 Document 身份 |
| access_scope | 宿主构造的可信授权范围，见「共同约束」 |
| idempotency_key | 幂等键；(namespace, ingest, idempotency_key) 内唯一 |

返回 IngestionJobRef：job_id（持久任务引用）+ 当前状态（queued / running / ready / failed / needs_review / cancelled）。

### get_ingestion

| 响应字段 | 说明 |
|---|---|
| stage / progress | 当前处理阶段与完成计数 |
| error_code | 失败或需人工介入的错误码 |
| attempt_count | 已尝试次数 |
| warnings | 非致命问题（如解析降级） |
| revision_id | 成功构建的修订；ready 前为空 |
| preview | 可预览来源（同样鉴权） |

### publish

| 参数 | 说明 |
|---|---|
| revision_id | 仅接受 ready 且在当前索引代构建完整的修订 |
| valid_from / valid_until | 有效区间 [from, until)；until 为 null 表示无限；append/replace-tail 语义见上文 |
| expected_generation | Document 发布状态的并发控制版本（不是 IndexGeneration）；不匹配返回 ConcurrentModification |
| namespace / access_scope / idempotency_key | 同 ingest |

### withdraw / set_access / delete

| 接口 | 参数 | 说明 |
|---|---|---|
| withdraw | document_id, expected_generation, access_scope, idempotency_key | 置撤回标记并递增 generation；不删历史 |
| set_access | document_id, acl, expected_generation, … | 原子更新当前 ACL 并递增状态版本；不随内容回滚 |
| delete | document_id, … | 先不可检索，再后台清理；删除审计不含正文 |

### retrieve

| 参数 | 说明 |
|---|---|
| query | 自然语言查询原文；改写仅作内部受控扩展 |
| namespace | 限定命名空间 |
| access_scope | 权限过滤；返回前再次检查 |
| at | 按发布记录查询的业务时刻；默认当前；不允许晚于当前时刻 |
| applicability | 产品、地区、渠道等适用条件；文档声明而请求缺失时返回 InvalidArgument |
| top_k | 最多返回条数，建议 1–20；不保证凑满 |
| deadline_ms | 本次检索预算 |

RetrievalResult.evidence 单条字段：

| 字段 | 说明 |
|---|---|
| text | 原文片段，与原文逐字对应 |
| document_id / revision_id / chunk_id | 身份定位三件套 |
| publication_window | 命中时适用的 Publication 有效区间 |
| heading_path | 标题路径 |
| source_locator | 不可变原文引用 + 原文区间；支持时附章节、页码、表格行号 |
| context_parts[] | 补充上下文；每项有逐字对应原文的 `text` 和独立的 `source_locator` |
| warning_codes[] | 该条证据的机器可读警告码；缺少关键上下文时包含 `evidence_incomplete` |

其余顶层字段（rank_score / score_type、index_generation / model_version、degraded / warnings、trace_id / stage_timings）见「retrieve 与 Evidence」一节的语义表。

## 错误与资源生命周期

| 错误 | 调用方处理 |
|---|---|
| InvalidArgument / UnsupportedSource | 修正输入，不自动重试 |
| NotFoundOrForbidden / AccessDenied | 核验身份和权限，不退到无权限模式 |
| IdempotencyConflict / PublicationConflict / ConcurrentModification | 读取当前状态并由业务明确选择，不能盲目覆盖 |
| DependencyUnavailable / RateLimited | 在总 deadline 内有限重试，尊重 retry-after |
| DeadlineExceeded | 本次未完成；不要解释为库中无知识 |
| IngestionFailed / NeedsReview | 查看逐项原因，修复来源或人工确认 |

客户端应支持连接池复用、关闭和请求取消；异步宿主使用非阻塞适配或受限线程池，禁止在事件循环里直接运行阻塞模型推理。失败分类和预算见 [运行保障](operations.md)。

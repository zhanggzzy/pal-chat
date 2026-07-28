# pal-chat 首版架构基线

状态：提议评审  
关联议题：OT-19  
输入基线：`docs/project-charter.md`（提交 `c7fe416`）  
适用范围：单用户、双 Agent、本地运行的可观察群聊 Demo

## 1. 决策摘要

首版采用一个前后端分离、后端模块化单体的本地应用：

- 前端：TypeScript、React、Vite。聊天区和分析区共享同一份查询缓存与 SSE
  事件归并器，不引入全局状态框架。
- 后端：Python、FastAPI。HTTP API、话题路由、Agent 编排和模型适配运行在同一
  进程，代码按模块分层，不拆微服务。
- 实时通信：命令与历史查询使用 REST；服务端状态变化使用单向 SSE。
- 数据库：SQLite WAL、SQLAlchemy、Alembic。数据库是消息、话题、决策和事件的
  唯一事实来源。
- 模型接入：仓库内定义小型 `ModelGateway` 接口，供应商 SDK 只存在于适配器；
  不引入通用 Agent 框架。
- 编排：每条消息触发一个“决策轮”。两个 Agent 基于各自不可变上下文快照并行
  判断，服务端在决策屏障后最多选择一个发言者，生成的回复作为下一条消息进入
  新一轮。
- 话题：一条消息有且仅有一个主话题，可有一个只用于解释的次要关联；任一时刻
  只有一个 active 主话题。路由采用规则约束下的轻量模型分类。
- 硬限制默认值：每次用户触发最多 5 条 Agent 消息、6 个决策轮、30,000 个总
  Token、90 秒墙钟时间、同一 Agent 最多连续发言 2 次。任一限制先到即停止。

这些默认值是配置，不是产品承诺；第 14 节列出需要产品负责人确认的内容。

## 2. 范围与设计原则

### 2.1 首版必须证明

1. 两个 Agent 在每条新消息后分别选择回应或沉默，而非固定轮流。
2. 话题 A 被 B 打断后仍可保留，并在后续消息中重新激活。
3. 任一 Agent 的实际输入、话题选择、决策结果和调用成本可被逐步复盘。
4. 自动接力一定会因无人回应、手动停止或硬限制而结束。
5. 页面刷新后能读取已提交的历史，不承诺恢复中断的模型调用。

### 2.2 首版刻意不做

- 微服务、消息队列、分布式锁、向量数据库和多进程 worker。
- 通用 Agent 定义、工具调用、长期记忆、RAG、账户与多租户。
- 展示或存储模型内部思维链。
- 节点重跑、运行分叉、逐 Token 回放和严格可复现。
- 自动恢复进程崩溃时尚未完成的模型调用。

### 2.3 设计原则

- **先提交再通知**：只有数据库事务提交后的事实才通过 SSE 展示。
- **快照不可变**：决策使用的上下文一经记录不被后续话题修正改写。
- **单会话串行**：同一会话只有一个自动接力运行，减少首版竞态。
- **模型提建议，规则守边界**：模型参与语义分类；预算、状态和合法引用由服务端
  校验。
- **沉默也是结果**：每个 Agent 的沉默决策和未被选中结果都持久化。
- **失败时停止**：无法确认是否安全继续时，不自动补发消息。

## 3. 最小组件边界

```text
┌──────────────────── Browser SPA ────────────────────┐
│ Chat UI │ Analysis UI │ REST client │ SSE reducer   │
└───────────────┬───────────────────────▲──────────────┘
                │ REST commands/query   │ committed events
┌───────────────▼───────────────────────┴──────────────┐
│                 FastAPI modular monolith             │
│                                                     │
│ API ── Application services ── Conversation Runner  │
│          │          │             │                 │
│     Topic Router  Context Builder  Decision Policy  │
│          │          │             │                 │
│          └──────── ModelGateway adapters ───────────┼──► LLM API
│                                                     │
│ Repository layer ── Event journal/SSE broadcaster   │
└────────────────────────┬────────────────────────────┘
                         │ transactions
                    ┌────▼─────┐
                    │ SQLite   │
                    │ WAL file │
                    └──────────┘
```

### 3.1 前端

前端只负责呈现、输入和本地交互状态，不推断领域状态：

- `ChatPane`：消息、发送、生成状态、停止按钮。
- `AnalysisPane`：运行时间线、话题状态、Agent 索引、快照、决策和指标。
- `EventReducer`：按单调递增 `seq` 合并 SSE；发现序号缺口时重新取会话快照。
- `ApiClient`：REST 命令和历史查询；发送命令携带幂等键。

服务端返回的 `run.status`、`topic.status` 和 `decision.outcome` 才是事实；前端不以
动画或请求是否结束猜测它们。

### 3.2 API 与应用服务

- 校验请求、幂等键和当前会话状态。
- 打开事务，调用领域模块，提交事实和事件。
- 不包含模型供应商特有字段。
- SSE 端点只读取事件日志并订阅提交后的内存通知。

### 3.3 Conversation Runner

- 每个会话最多一个进程内异步 runner，以会话锁串行执行。
- 执行话题路由、索引更新、上下文快照、双 Agent 决策、发言者选择和回复生成。
- 每个有副作用的步骤前后检查 `run.status`、取消版本和预算。
- runner 不是事实来源；进程重启后以数据库将未结束运行安全终止。

### 3.4 领域模块

- `TopicRouter`：给候选话题打分，返回 `continue | reactivate | create`。
- `AgentTopicIndexer`：维护 Agent 到中心话题的排序元数据，不复制话题事实。
- `ContextBuilder`：在 Token 预算内选择消息和摘要，产出不可变快照。
- `DecisionPolicy`：校验模型返回的维度，由服务端计算参与分和阈值结论。
- `SpeakerSelector`：在决策屏障后确定至多一个发言者。
- `BudgetGuard`：在每次调用前预留预算，调用后以供应商实际用量结算。

### 3.5 ModelGateway

```python
class ModelGateway(Protocol):
    async def classify_topic(self, request: TopicRouteRequest) -> ModelResult: ...
    async def score_participation(self, request: DecisionRequest) -> ModelResult: ...
    async def generate_reply(self, request: GenerationRequest) -> ModelResult: ...
    async def summarize_topic(self, request: SummaryRequest) -> ModelResult: ...
```

`ModelResult` 统一包含供应商、模型、请求 ID、结构化输出、输入/输出 Token、耗时和
错误类别。上层只依赖本接口。超时、重试和结构化输出校验放在适配器外的共同包装
层，避免各供应商行为漂移。

## 4. 端到端数据流

### 4.1 用户消息与自动接力

```text
User       API/DB       Runner       Router/Context       Agent A/B       SSE/UI
 │ POST msg  │             │                │                 │              │
 ├──────────►│ tx: message + run + event    │                 │              │
 │◄──────────┤ 202 {message_id, run_id}     │                 │              │
 │           ├────────────►│                │                 │              │
 │           │             ├─ route topic ─►│                 │              │
 │           │             ├─ persist topic/link/index/event │              │
 │           │             ├─ build two immutable snapshots ─┤              │
 │           │             ├──────── decisions in parallel ──► A and B      │
 │           │             ├─ persist both decisions + calls │              │
 │           │             ├─ select at most one speaker     │              │
 │           │             ├─ capture generation snapshot    │              │
 │           │             ├──────── generate reply ─────────► selected     │
 │           │             ├─ tx: agent message + event      │              │
 │           │             └─ next decision round ───────────┘              │
 │           │        stop when nobody replies / cancelled / limit / error  │
 │           │             └─ tx: final run status + reason                 │
 │           └──────────────── committed events ───────────────────────────►│
```

响应决策调用可以并行，因为它们只读取各自快照。发言生成和消息提交串行，因此不
会出现两个回复同时以旧上下文写入。若两个 Agent 都想回应，较高分者本轮发言；
另一方记录为 `eligible_not_selected`，并在新消息到来后重新判断。

### 4.2 话题 A → 打岔 B → 回到 A

```text
M1 用户谈 A
  Router: create A
  State: A active
  Snapshots: A 尾部消息

M2 Agent 回复 A
  Router: continue A
  State: A active

M3 用户说 “by the way ...B...”
  Router: create B, interrupted_topic_id=A
  Transition: A(active→inactive), B(new→active), cause=M3
  Snapshots: B 当前消息 + 少量全局过渡上下文；A 只保留在索引中

M4 Agent 回复 B
  Router: continue B

M5 用户重新提到 A 的实体/问题
  Router: reactivate A
  Transition: B(active→inactive), A(inactive→active), cause=M5
  Snapshots: A 摘要 + A 最近消息 + M5 + 少量全局过渡上下文
```

分析区能从 `message_topic_links` 解释每条消息落到何处，从
`topic_transitions` 重放 active/inactive 变化，从 `context_snapshots` 证明恢复 A
时实际加入了哪些内容，从 `response_decisions` 解释两个 Agent 的回应或沉默。

## 5. 话题链首版策略

### 5.1 主关联与次要关联

每条消息：

- 必须有一个 `primary` 话题，驱动 active 状态、摘要和默认上下文。
- 最多有一个 `secondary` 话题，只表达桥接或弱相关，不改变 active 状态。

相比“任意多个等价话题”，这使状态机和复盘明确；相比“严格单话题”，又能保留
“从 B 顺便提回 A”这种过渡信息。若分类器返回更多关联，服务端只保留置信度最高
的一个次要关联并记录截断原因。

### 5.2 单 active 话题

首版任一时刻只有一个 active 主话题，存于 `conversations.active_topic_id`，并由
事务同时写入状态变化。原因：

- A → B → A 验收不要求真正并行的多个注意焦点。
- 多 active 会迫使上下文预算、失活计时和 UI 语义同时复杂化。
- 次要关联和 Agent 索引已能表达“仍然记得其他话题”。

不按时间自动失活。只有 `create` 或 `reactivate` 另一个主话题时，旧主话题才
失活；这样本地 Demo 不受运行速度影响。

### 5.3 混合路由

候选集合限制为：

1. 当前 active 话题；
2. 最近更新的最多 5 个 inactive 话题；
3. 每个 Agent 索引中得分最高且尚未入选的最多 2 个话题。

模型只看到候选的 ID、短标题、最新摘要和少量锚点消息，返回：

```json
{
  "action": "continue | reactivate | create",
  "primary_topic_id": "existing-id-or-null",
  "secondary_topic_id": "existing-id-or-null",
  "confidence": 0.0,
  "signals": ["entity_overlap", "explicit_callback"],
  "proposed_title": "new topic only"
}
```

服务端规则随后校验：

- `continue` 只能指向 active 话题；
- `reactivate` 只能指向候选中的 inactive 话题；
- ID 必须来自候选集合，主次不得相同；
- 低于 `0.55` 或输出无效时：若与 active 话题存在明确词面重合则继续，否则创建
  新话题；
- 每次路由只允许一次结构修复重试，仍失败则使用上述规则回退。

所有候选、模型结果、规则覆盖、置信度和 `policy_version` 均记录。`signals` 是短
标签，不接受自由形式思维链。

### 5.4 话题摘要

- 每个话题保留不可变的摘要修订，当前话题指向最新修订。
- 当未摘要消息超过 8 条或预计超出上下文预算时异步于本轮内更新摘要；摘要失败
  不阻塞聊天，退化为最近消息截断。
- 摘要只总结已提交消息，保存覆盖到的最后消息 ID 和模型调用。
- 历史上下文快照引用具体摘要修订，因此以后更新摘要不会改写过去。

### 5.5 人工修正边界

首版必须可看见误分类：分析区展示候选、置信度、规则覆盖和话题转换。默认不把
通用话题编辑器纳入 P0。若产品确认需要演示修正，只增加一个本地实验操作：
“更正此消息的主话题”。

该操作必须追加 `manual_topic_override` 和新的话题转换事件；它只影响后续路由和
索引，不重跑、不删除原决策、不改写既有上下文快照。完整的历史重算和分叉仍是
非目标。

## 6. Agent 索引、上下文与 Token 控制

### 6.1 Agent 话题索引

`agent_topic_indexes` 只引用中心 `topic_id`，保存：

- `salience`：该 Agent 当前对话题的关注度，0～1；
- `role_affinity`：话题与角色设定的匹配度，0～1；
- `familiarity`：实际读过该话题上下文的程度，0～1；
- `last_seen_message_id`、`last_selected_at`；
- `index_version` 和短 `reason_codes`。

路由完成后，服务端用确定性衰减加模型给出的角色匹配更新索引。索引不保存消息
正文或另一份话题摘要，避免与中心事实分叉。

### 6.2 上下文组装顺序

每个 Agent 独立执行，预算默认 4,000 输入 Token：

1. 固定系统安全边界与角色设定；
2. 决策任务的结构化输出契约；
3. 当前触发消息；
4. 主话题的最新摘要修订；
5. 主话题最近消息，从近到远加入；
6. 最近 2 条全局消息，补充打岔过渡；
7. 该 Agent 索引最高的最多 2 个其他话题，只加入标题与摘要；
8. 为输出预留 400 Token，并保留 10% 估算误差余量。

空间不足时按 7 → 6 → 5 的旧消息顺序裁剪，1～4 不裁剪。若 1～4 已超预算，拒绝
本次调用并以 `context_budget_exceeded` 停止，而不是静默截断用户当前消息。

### 6.3 快照

`context_snapshots` 保存稳定、可复盘的“实际输入表示”，包括：

- 有序条目及角色；
- 消息或摘要修订引用；
- 系统模板和策略版本；
- 供应商 tokenizer 名称；
- 每条估算 Token、总估算 Token；
- 发送给供应商的规范化内容哈希。

为本地实验复盘，数据库可保存规范化提示正文，但常规日志不得打印。若以后处理
敏感数据，应增加“仅保存引用/加密正文”模式；这不在首版范围。

### 6.4 Token 与成本

- 调用前使用与目标模型匹配的 tokenizer；无可用 tokenizer 时使用保守字符估算
  并把 `token_count_source=estimate` 写入记录。
- 调用后以供应商返回的 input/output/cached Token 覆盖结算值；没有用量时保留
  估算并标记来源。
- 价格不硬编码在调用逻辑。`pricing_catalog` 按供应商、模型和生效时间保存本地
  配置快照，`model_calls` 保存当次价格版本和估算成本。
- `BudgetGuard` 在调用前预留“估算输入 + 最大输出”，防止并行的两个决策调用
  同时穿透总预算；调用完成后释放差额。

## 7. 回应/沉默决策

### 7.1 结构化信号

模型为每个 Agent 返回 0～1 维度和枚举原因：

```json
{
  "relevance": 0.86,
  "direct_address": 0.0,
  "novelty": 0.72,
  "role_fit": 0.90,
  "open_question": 0.60,
  "repetition_risk": 0.10,
  "reason_codes": ["ROLE_EXPERTISE", "HAS_NEW_INFORMATION"],
  "reply_intent": "answer | ask | add | challenge | continue | none"
}
```

服务端计算而非让模型自报最终分：

```text
score =
  0.30 × relevance
+ 0.20 × direct_address
+ 0.20 × novelty
+ 0.15 × role_fit
+ 0.15 × open_question
- 0.20 × repetition_risk
- 0.15 × recent_speaker_penalty
```

结果截断到 `[0, 1]`。默认阈值 `0.62`，两个 Agent 可有独立阈值，但同一运行内不
变化。`should_reply = eligible AND score >= threshold`。

### 7.2 硬资格规则

以下规则优先于分数，并记录为结构化原因：

- 运行非 `running`、已请求停止或预算不足：`RUN_NOT_ELIGIBLE`。
- 同一 Agent 已连续发言 2 次：`CONSECUTIVE_LIMIT`。
- 当前内容与其最近回复的归一化相似度超过 `0.92`：`REPETITION_GUARD`。
- 决策结构两次均无效：`INVALID_MODEL_OUTPUT`，本轮按沉默处理。

允许同一 Agent 在自己消息后再判断一次，从而支持自然的连续补充，但连续上限
阻止自我循环。该产品语义需在第 14 节确认。

### 7.3 发言者选择

双 Agent 决策全部落库后再选择：

1. 无人 `should_reply`：以 `no_candidate` 正常结束运行；
2. 只有一人：选择该 Agent；
3. 两人都想回复：高分者优先；
4. 分差小于 `0.03`：较久未发言者优先；
5. 仍相同：按稳定的 `participant_id` 排序，保证可解释。

决策 `outcome` 分为 `silent`、`selected`、`eligible_not_selected`、
`ineligible`。因此“想说但本轮没抢到”不会被误显示为沉默。

### 7.4 校准

用固定的 A → B → A 对话夹具记录期望的相对行为，不追求模型输出逐字一致：

- 明确点名时相关 Agent 分数应高于未点名 Agent；
- 无新信息、重复上一回复时应低于阈值；
- 角色专长命中应提高 `role_fit`；
- 两个 Agent 的发言次数不要求相等，但不得固定 A/B 轮流；
- 一次验收运行目标 3～5 条 Agent 消息。

阈值和权重每次修改都升级 `decision_policy_version`。历史记录保存原版本，禁止用
新权重重算后覆盖旧结果。

## 8. 自动接力状态机与并发

### 8.1 状态机

```text
                 ┌──────────── user stop / hard limit / fatal error ───────┐
                 │                                                         ▼
created → routing → indexing → deciding → selecting → generating → committing
              ▲                         │              │              │
              │                         │ no speaker   │ failed       │ new message
              │                         ▼              ▼              │
              └────────────────────── stopped ◄────────┴──────────────┘
```

数据库中的 `chat_runs.status` 使用粗粒度状态：

- `queued`
- `running`
- `stop_requested`
- `stopped`
- `failed`
- `completed`

细粒度步骤由 `run_events` 表达，不把瞬时网络状态塞进运行状态。终态必须有
`stop_reason`：

`no_candidate | user_stop | max_agent_messages | max_rounds |
max_total_tokens | timeout | provider_error | invalid_output |
server_restart | context_budget_exceeded`。

### 8.2 一轮的原子边界

一轮包括：

1. 为触发消息路由话题并更新索引；
2. 写两个上下文快照；
3. 并行运行两个决策调用；
4. 同一事务写两个决策并选择发言者；
5. 为选中 Agent 写一份 generation 快照并串行生成；
6. 同一事务写模型调用、Agent 消息、计数和事件。

外部模型调用不持有数据库事务。每次调用前读运行的 `cancel_generation`；结果
回来后用 compare-and-set 再校验。若版本已变化，保存调用指标但丢弃候选回复，
绝不创建消息。

### 8.3 重复触发防护

- `POST /messages` 要求 `Idempotency-Key`；唯一键为
  `(conversation_id, idempotency_key)`。
- 同一会话只允许一个非终态运行；数据库唯一约束作为进程锁之外的第二道防线。
- 每个 Agent 每条触发消息只有一个决策：
  `UNIQUE(run_id, trigger_message_id, agent_id)`。
- Agent 消息携带 `caused_by_decision_id`，该列唯一，避免重试时重复写消息。
- SSE 事件有会话内单调 `seq`，客户端按 ID 去重。

### 8.4 手动停止

`POST /runs/{id}/stop` 立即把运行置为 `stop_requested` 并递增
`cancel_generation`：

- 尚未开始的调用不再启动；
- 可取消的 HTTP 调用主动取消；
- 无法取消的调用可以返回并记录用量，但版本校验阻止其回复落库；
- 事务已经提交的消息不会撤回；
- runner 最终写 `stopped/user_stop`。

停止接口幂等。UI 收到 `run.stopped` 前显示“正在停止”，而不是假装已完成。

### 8.5 重启恢复

首版不恢复半完成接力。服务启动时：

1. 找出 `queued/running/stop_requested` 运行；
2. 将它们终结为 `failed/server_restart`；
3. 释放预算预留，追加事件；
4. 保留所有已提交消息、快照、决策和调用。

用户可从历史复盘，随后发送新消息开启新运行。该策略比猜测供应商请求是否已经
成功更安全。

## 9. 数据模型

所有 ID 使用 UUID；时间存 UTC；JSON 只承载演进快、无需关系查询的结构化维度，
核心关系保持规范化。

```text
conversations
  id, title, active_topic_id?, created_at, updated_at

participants
  id, conversation_id, kind(user|agent), display_name,
  role_prompt?, decision_threshold?, sort_order

chat_runs
  id, conversation_id, root_user_message_id, status, stop_reason?,
  max_agent_messages, max_rounds, max_total_tokens, timeout_ms,
  agent_message_count, round_count, reserved_tokens, actual_tokens,
  cancel_generation, started_at?, ended_at?

messages
  id, conversation_id, run_id, author_participant_id,
  parent_message_id?, caused_by_decision_id?, kind(user|agent),
  content, sequence_no, created_at

topics
  id, conversation_id, title, status(active|inactive),
  interrupted_topic_id?, current_summary_revision_id?,
  created_by_message_id, created_at, updated_at

message_topic_links
  id, message_id, topic_id, kind(primary|secondary), confidence,
  route_action, route_method(model|rule|manual), signals_json,
  policy_version, is_override, created_at

topic_transitions
  id, conversation_id, from_topic_id?, to_topic_id,
  cause_message_id, action(create|switch|reactivate|manual_override),
  previous_status_json, created_at

topic_summary_revisions
  id, topic_id, content, through_message_id, model_call_id?,
  estimated_tokens, created_at

agent_topic_indexes
  agent_id, topic_id, salience, role_affinity, familiarity,
  last_seen_message_id?, last_selected_at?, reason_codes_json,
  index_version, updated_at

context_snapshots
  id, run_id, trigger_message_id, agent_id, purpose(decision|generation),
  policy_version, tokenizer, estimated_tokens, canonical_content_hash,
  created_at

context_snapshot_items
  id, snapshot_id, ordinal, item_type(system|message|topic_summary|instruction),
  message_id?, summary_revision_id?, rendered_content,
  estimated_tokens, inclusion_reason

response_decisions
  id, run_id, trigger_message_id, agent_id, snapshot_id, model_call_id?,
  eligible, should_reply, score, threshold, dimensions_json,
  reason_codes_json, reply_intent, outcome, policy_version, created_at

model_calls
  id, run_id, purpose(topic_route|decision|generation|summary),
  agent_id?, provider, model, provider_request_id?, status,
  input_tokens, output_tokens, cached_tokens, token_count_source,
  latency_ms, pricing_version?, estimated_cost_micros?,
  error_category?, started_at, ended_at

run_events
  id, conversation_id, run_id?, seq, event_type, entity_type,
  entity_id, public_payload_json, created_at

request_idempotency
  conversation_id, key, request_hash, response_status,
  response_body_json, created_at
```

### 9.1 关键不变量

- 一个会话至多一个 active 话题；`active_topic_id` 与话题状态在同一事务维护。
- 每条消息恰有一个主话题链接。
- 每条 Agent 消息必须引用唯一的 selected 决策。
- 快照条目只能引用与该会话相同的消息/摘要。
- 事件 `seq` 在会话内唯一且递增。
- 运行计数只在消息或调用结算事务内增加。
- 已进入终态的运行不得再次回到 `running`。
- 历史快照、决策和模型调用只追加，不原地重算。

## 10. API 与事件契约

路径以 `/api/v1` 为前缀；实际实现以 OpenAPI 作为可执行契约。

### 10.1 命令

```http
POST /conversations
→ 201 { conversation, participants }

DELETE /conversations/{conversation_id}
→ 204

POST /conversations/{conversation_id}/messages
Idempotency-Key: <uuid>
{ "content": "..." }
→ 202 { "message_id": "...", "run_id": "...", "status": "queued" }

POST /runs/{run_id}/stop
Idempotency-Key: <uuid>
→ 202 { "run_id": "...", "status": "stop_requested|stopped" }
```

发送消息时若会话已有非终态运行，返回 `409 run_in_progress`。首版不排队用户
消息，用户应先停止或等待本轮结束。

若产品确认人工更正：

```http
PATCH /messages/{message_id}/primary-topic
Idempotency-Key: <uuid>
{ "topic_id": "...", "reason": "local experiment correction" }
→ 200 { "message_id": "...", "primary_topic_id": "...", "effective_from_seq": 42 }
```

### 10.2 查询

```http
GET /conversations/{id}
GET /conversations/{id}/messages?after_sequence=&limit=
GET /conversations/{id}/topics
GET /topics/{id}?include=transitions,summaries
GET /messages/{id}/analysis
GET /runs/{id}
GET /runs/{id}/timeline?after_seq=
```

`GET /messages/{id}/analysis` 一次返回路由结果、两个 Agent 的索引视图、快照引用、
决策、模型调用和 speaker selection，避免分析区拼接多个时序不一致的请求。

### 10.3 SSE

```http
GET /conversations/{id}/events?after_seq=41
Accept: text/event-stream
Last-Event-ID: 41
```

事件外壳：

```json
{
  "event_id": "uuid",
  "seq": 42,
  "type": "decision.completed",
  "conversation_id": "uuid",
  "run_id": "uuid",
  "entity_id": "uuid",
  "occurred_at": "2026-07-28T10:00:00Z",
  "schema_version": 1,
  "data": {}
}
```

首版事件：

- `message.created`
- `topic.routed`
- `topic.status_changed`
- `agent_index.updated`
- `context.captured`
- `decision.completed`
- `speaker.selected`
- `generation.started`
- `generation.completed`
- `run.stop_requested`
- `run.completed`
- `run.stopped`
- `run.failed`

事件 `data` 只包含 UI 立即呈现所需的非敏感字段；完整提示正文通过鉴权后的历史
查询读取。SSE 重连先从 `run_events` 补齐，再切换到内存订阅，不能只依赖内存
广播。

### 10.4 错误外壳

```json
{
  "error": {
    "code": "run_in_progress",
    "message": "A relay run is already active.",
    "request_id": "uuid",
    "details": {}
  }
}
```

错误码稳定，面向人的 message 可调整。请求 ID 贯穿 API 日志、模型调用和事件。

## 11. 分析区来源映射

| 展示内容 | 事实来源 |
| --- | --- |
| 消息和生成状态 | `messages`、`chat_runs`、`generation.*` |
| active/inactive 话题 | `topics`、`topic_transitions` |
| 创建/继续/重新激活原因 | `message_topic_links` |
| Agent 眼中的话题索引 | `agent_topic_indexes` |
| 实际上下文条目和 Token | `context_snapshots`、`context_snapshot_items` |
| 回应/沉默、分数、阈值 | `response_decisions` |
| 想回应但未选中 | `response_decisions.outcome`、`speaker.selected` |
| 模型、耗时、Token、成本 | `model_calls` |
| 循环计数和终止原因 | `chat_runs`、`run_events` |

分析区不展示隐藏推理。允许展示的“原因”仅来自已定义的信号、分数维度、规则命中
和短原因码。

## 12. 非功能、安全与运行

### 12.1 可验证目标

| 属性 | 首版目标 |
| --- | --- |
| 本地 API 延迟 | 不含模型调用的 p95 < 200 ms |
| UI 可见性 | 事务提交后 500 ms 内发出 SSE 事件 |
| 数据完整性 | SSE 可见事实 100% 已落库；Agent 消息 100% 关联决策 |
| 停止边界 | stop 提交后不再落库任何新 Agent 消息 |
| 预算边界 | 不启动会使预留 Token 超限的新调用 |
| 重复防护 | 同一幂等键/决策重试不产生重复消息 |
| 可复盘性 | 每个决策都能关联快照、策略版本和调用指标 |
| Demo 容量 | 1 个浏览器会话、2 个 Agent、单会话串行运行 |

模型供应商延迟和不可取消请求不计入本地 API p95；它们单独记录。

### 12.2 Secret 与配置

- 后端从环境变量读取模型密钥；本地 `.env` 仅由开发者创建并在 `.gitignore`
  排除，仓库提供无值的 `.env.example`。
- 前端永不持有供应商密钥，也不允许把任意 base URL/密钥通过聊天请求透传。
- 配置分成非敏感 `config.toml` 默认值与环境变量 Secret；启动时校验并打印配置
  名称，不打印值。
- SQLite、日志和导出文件默认存放在仓库外的用户数据目录；仓库只保存 schema。

### 12.3 安全边界

- 首版只监听 loopback；CORS 只允许本地前端 origin。若绑定非 loopback 必须先
  增加身份认证，本架构不声称当前可安全公网暴露。
- 用户与 Agent 消息作为带标签的数据块传给模型，不拼接成系统指令；系统提示
  明确禁止把消息内容当工具或高优先级指令。
- Agent 没有工具、文件、网络或代码执行能力，提示注入的副作用限制为生成文本。
- 输入长度、请求体、SSE 连接数和输出 Token 均设上限。
- 常规日志只写 ID、状态、耗时、Token 和错误类别；正文、提示、密钥及供应商
  原始响应不进日志。
- 面向分析区的完整快照可能含用户内容，必须明确标为本地实验数据并提供删除整个
  会话的能力；首版不做字段级权限。
- 模型供应商的数据保留政策是外部信任边界，选定供应商前由产品负责人确认是否可
  发送演示内容。

### 12.4 可观测性

- 结构化 JSON 日志字段：`request_id`、`conversation_id`、`run_id`、
  `round_no`、`model_call_id`、`event_type`、`duration_ms`、`error_category`。
- 本地 `/health/live` 只证明进程存活；`/health/ready` 检查数据库可写和 schema
  版本，不调用模型供应商。
- 运行级指标从数据库查询生成，不引入 Prometheus：调用数、成功率、p50/p95
  延迟、Token、成本、决策分布、停止原因。
- 开发日志保留最近文件并轮转；历史复盘依赖数据库而非日志。

### 12.5 本地运行

建议仓库结构：

```text
apps/web/          React SPA
apps/server/       FastAPI entrypoint and API
packages/contracts/ generated OpenAPI TypeScript client/schema
docs/              charter, architecture, ADRs
var/               ignored local DB/logs when explicitly configured
```

开发使用两个前台进程（Vite、单 worker FastAPI），由一个简单任务文件或脚本
并发启动；不要求 Docker。首版禁止启动多个 API worker，因为会话锁只在进程内。
可选 Docker Compose 只用于统一环境，不增加数据库或队列容器。

## 13. 故障模式与缓解

| 故障模式 | 可观察信号 | 首版行为 | 验证 |
| --- | --- | --- | --- |
| 模型超时/限流 | `model_calls.error_category` | 至多一次带抖动重试；仍失败则终止运行 | 故障注入适配器 |
| 结构化输出无效 | 校验错误、修复次数 | 一次修复；路由走规则回退，决策按沉默或终止 | 固定坏 JSON |
| 两 Agent 同时想回复 | 两个高分决策 | 分数/最近发言/稳定 ID 决定一人 | 决策夹具 |
| 重复 HTTP 请求 | 相同幂等键 | 返回原响应，不建新消息/运行 | 并发重复请求 |
| 多标签页同时发送 | 非终态运行唯一约束 | 一个成功，一个 `409` | 并发集成测试 |
| SSE 断线/漏事件 | `seq` 不连续 | 按 Last-Event-ID 补齐，必要时重取快照 | 断网重连测试 |
| 用户停止遇到在途调用 | 取消版本变化 | 记录用量但丢弃迟到回复 | 延迟 fake provider |
| server 崩溃 | 非终态运行遗留 | 启动时标 `failed/server_restart`，不自动重放 | kill/restart 测试 |
| SQLite 忙/磁盘满 | DB 错误分类 | 短 busy timeout；写失败不发 SSE，停止运行 | 锁库/限额测试 |
| 话题误分类 | 低置信度/人工观察 | 保留证据；可选追加修正，不改历史快照 | A/B/A 路由夹具 |
| 摘要漂移 | 摘要修订与覆盖范围 | 快照钉住修订；失败退化最近消息 | 摘要失败测试 |
| Token 估算偏低 | estimate 与 actual 差值 | 10% 余量；调用后结算；不再开新调用 | tokenizer 对照测试 |
| Agent 重复/自循环 | 相似度和连续计数 | 重复保护、连续 2 次上限、全局硬限制 | 回声模型测试 |
| 日志泄露正文/Secret | 日志审计 | 字段白名单与脱敏测试 | canary secret 测试 |

## 14. 产品评审决定

产品负责人于 2026-07-28 接受架构基线，并确认：

1. 同一 Agent 可以在自己的消息后连续补充，最多连续 2 次。
2. “更正消息主话题”不属于 P0；首版只观察误分类，不提供编辑 UI。
3. 分析区显示未被选中的高分 Agent 为“想回应但未选中”。
4. 首版采用 5 条 Agent 消息、6 轮、30,000 Token、90 秒作为初始硬上限；数值
   通过后端配置调整并在 UI 可见，运行中不可扩大。
5. 首版提供删除单个会话的能力。
6. 具体模型供应商、轻量模型及数据保留政策仍是实现前配置项；契约、schema、
   fake provider 与 UI 设计不得依赖某一商业供应商，真实模型适配开始前再确认。

## 15. ADR：关键选型与权衡

### ADR-001：模块化单体，而非微服务

- **选择**：FastAPI 单进程，模块边界由包和接口表达。
- **备选**：模型 worker/编排服务独立部署；消息队列驱动。
- **理由**：单用户串行 Demo 没有独立伸缩或故障隔离收益；拆分只会引入分布式
  事务、部署和追踪成本。领域接口保留未来拆分路径。

### ADR-002：Python/FastAPI，而非 Node 或 Go

- **选择**：Python/FastAPI、Pydantic、SQLAlchemy/Alembic。
- **备选**：Node/Fastify 可实现全栈 TypeScript；Go 有更小运行时和强并发。
- **理由**：Python 的模型 SDK、结构化输出和 tokenizer 生态更直接；FastAPI
  自带 OpenAPI，足以支撑本地异步 I/O。首版并不需要 Go 的吞吐。
- **代价**：前后端语言不同；用 OpenAPI 生成 TypeScript 契约缓解。

### ADR-003：React/Vite SPA 与轻量状态

- **选择**：React、Vite、TypeScript；服务端状态通过查询缓存 + SSE reducer。
- **备选**：Next.js；Vue；Redux/Zustand。
- **理由**：不需要 SSR、路由服务端或复杂全局客户端状态。React/Vite 生态适合
  快速实现左右分栏实验台。状态库只在 reducer 明显失控后引入。

### ADR-004：REST + SSE，而非 WebSocket

- **选择**：REST 写入/查询，SSE 推送服务端事件。
- **备选**：WebSocket 双向协议；短轮询。
- **理由**：交互命令低频且适合 HTTP 幂等语义；实时流单向。SSE 原生支持事件
  ID、重连和代理友好性，比自定义 WebSocket 协议更小。
- **代价**：不适合未来多用户高频双向协作；该能力不在首版范围。

### ADR-005：SQLite WAL，而非 PostgreSQL

- **选择**：SQLite WAL、单写者短事务。
- **备选**：PostgreSQL；仅内存/JSON 文件。
- **理由**：无需额外服务，仍有事务、约束和可查询历史；比 JSON 文件更能保护
  不变量。单会话串行写入符合 SQLite 能力。
- **迁移信号**：需要多进程 worker、多用户并发写、远程部署或行级锁时再迁移
  PostgreSQL；在此之前不预置其运维成本。

### ADR-006：小型 ModelGateway，而非 Agent 框架

- **选择**：四个任务型方法、供应商适配器、统一指标包装。
- **备选**：直接散落供应商 SDK；LangChain 等通用 Agent 框架。
- **理由**：隔离供应商又保持调用链透明，便于记录真实输入与成本。Agent 没有
  工具和规划需求，通用框架会遮蔽本项目要观察的核心机制。

### ADR-007：混合话题路由与单 active

- **选择**：候选集 + 模型结构化分类 + 服务端规则；一个主话题 active。
- **备选**：纯关键词规则；embedding/向量检索；任意多个 active 话题。
- **理由**：纯规则难处理语义回指，向量库对小型短会话过重；混合方案既能演示
  A/B/A 又可解释、可回退。单 active 让上下文与 UI 状态明确。

### ADR-008：双决策屏障、单发言者

- **选择**：两个 Agent 并行独立决策，每轮最多一个 Agent 生成消息。
- **备选**：两个回复都并行生成；固定顺序依次询问；随机发言。
- **理由**：并行生成会基于同一旧上下文产生分支，固定顺序会偏置轮流感；屏障
  保留公平比较，再以可解释规则序列化消息。

### ADR-009：不可变快照与追加式事件

- **选择**：上下文、决策、调用和事件追加保存。
- **备选**：只保存当前派生状态；保存整段不可查询日志。
- **理由**：产品核心是复盘“当时实际发生了什么”。追加记录使后续摘要或策略
  更新不篡改历史，同时仍可用关系查询驱动分析区。

## 16. 迁移、演进与回滚

当前仓库没有业务数据，首个实现以 Alembic 建立 schema v1。之后遵循：

1. 先做向后兼容的新增表/列，应用读写新旧结构；
2. 本地启动迁移前备份 SQLite 文件；
3. 数据回填与删除旧列分开版本，不在一次迁移中完成；
4. 事件和策略都带版本，前端至少兼容当前与前一 schema version；
5. 回滚应用时使用兼容期内旧版本；若迁移不可逆，恢复迁移前数据库备份；
6. 模型适配器、话题策略和决策权重通过配置/版本切换，不改写历史数据。

渐进演进信号：

- 多用户或远程部署：先加身份认证与 PostgreSQL，再评估 worker；
- 会话数量导致候选检索变慢：先加数据库全文索引，证据充分后才考虑 embedding；
- SSE 不能满足明确的双向实时需求：保持 REST 命令，单独评估 WebSocket；
- 单进程模型调用阻塞：先引入受控进程内任务池，确定需要跨进程恢复后才引入
  持久队列。

## 17. P0 追踪矩阵

| P0 行为 | 组件 | 状态/事件 | 持久化责任 |
| --- | --- | --- | --- |
| 文本聊天与生成状态 | Chat UI、API、Runner | `message.created`、`generation.*` | `messages`、`model_calls` |
| 双 Agent 独立判断 | Context、Decision Policy | `decision.completed` | 两份 snapshot/decision |
| 非固定轮流与沉默 | Speaker Selector | `speaker.selected`、`no_candidate` | decision outcome、run reason |
| 中心话题 active/inactive | Topic Router | `topic.routed/status_changed` | topics、links、transitions |
| A → B → A | Router、Context | create/switch/reactivate | transitions、summary revisions |
| Agent 自己的话题索引 | Indexer | `agent_index.updated` | agent_topic_indexes |
| 实际上下文可见 | Context Builder | `context.captured` | snapshots/items |
| 有界自动接力 | Runner、Budget Guard | run completed/stopped/failed | run counters/reason |
| 用户随时停止 | API、Runner | stop requested/stopped | cancel generation/status |
| Token/耗时/成本 | Gateway wrapper | generation/decision events | model_calls、pricing version |
| 刷新后只读复盘 | Query API、Analysis UI | timeline replay | 所有追加式事实 |

## 18. 后续阶段拆分建议

以下是评审后的建议顺序，不在本任务中创建或启动子议题。

| 阶段 | 负责人角色 | 输入 | 输出 | 依赖 | 验收 |
| --- | --- | --- | --- | --- | --- |
| 1. 产品决策冻结 | 产品负责人（Picard） | 本文第 14 节 | 已确认默认值和 P0 修正边界 | 架构评审 | 每项开放决定有结论 |
| 2. 交互与信息架构 | UX（Ariadne） | 产品简报、事件/查询契约 | 聊天/分析区线框、停止和错误状态 | 阶段 1 | 每个 P0 事实有呈现位置 |
| 3. 契约与 schema | 后端开发（Neo） | 数据模型、OpenAPI 草案 | Alembic schema、OpenAPI、fake provider | 阶段 1 | 不变量与幂等集成测试通过 |
| 4. 话题/上下文策略 | 后端开发（Neo） | A/B/A 夹具、ModelGateway | Router、Indexer、Context Builder | 阶段 3 | A/B/A 产生可解释转换和快照 |
| 5. 决策与有界编排 | 后端开发（Neo） | 决策公式、运行状态机 | 双决策屏障、selector、stop/budget | 阶段 3、4 | 沉默、竞争、停止、上限均可测 |
| 6. Web 实验台 | 前端开发 | OpenAPI、SSE 事件、UX 线框 | 聊天区、分析区、历史复盘 | 阶段 2、3；可与 4/5 用 fake 并行 | 断线重连不重复/漏显示 |
| 7. 端到端验证 | 测试（Ripley） | 完整本地构建、验收夹具 | 风险矩阵对应测试与验收报告 | 阶段 4～6 | P0 矩阵及 A/B/A 场景全通过 |
| 8. Demo 校准 | 产品、测试、开发 | 验收报告、调用指标 | 阈值/预算版本与已知限制 | 阶段 7 | 3～5 条自然 Agent 消息且硬停止 |

实现风险给开发的硬约束：

- 不绕过数据库直接广播领域事件；
- 不在模型调用期间持有数据库事务；
- 不接受模型生成的任意 ID、状态或最终分数；
- 不把取消等同于“供应商一定未计费”；
- 不删除或覆盖历史快照来实现话题修正；
- 不在缺少可重复夹具时调整阈值与权重。

可测性对齐点：

- 所有模型调用必须可替换为可编程 fake（延迟、坏 JSON、限流、固定用量）；
- 时钟、ID 和 tokenizer 估算器可注入；
- 状态机步骤有数据库断言，不以 UI 文本作为唯一证据；
- A/B/A、双高分、全沉默、停止竞态、重启恢复是首批端到端夹具。

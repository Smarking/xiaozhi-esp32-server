# 可观测（OpenTelemetry + Jaeger）接入说明

给 `xiaozhi-server` 全链路（ASR/LLM/TTS）加 OpenTelemetry 埋点，trace 经 OTLP/HTTP 发往自托管 Jaeger，用于**每轮对话的分段时延分析 + LLM prompt/回答抽查**。本地、免费、零云依赖；未开启时全链路降级为 no-op，不影响主服务。

## 一轮对话的 trace 结构
```
conversation.turn   (session.id / device.id / turn.id / turn.end_reason / turn.aborted /
  │                  user.input / asr.text / turn.empty_asr)
  ├─ asr    (asr.duration_ms / asr.text / [wasted / waste.reason])
  ├─ llm    (gen_ai.request.model / gen_ai.system_instructions / gen_ai.prompt /
  │          gen_ai.completion / llm.ttfb_ms / llm.duration_ms + event:llm.first_token /
  │          gen_ai.usage.prompt_tokens|completion_tokens|cached_tokens|cache_hit_rate /
  │          [wasted / waste.reason])
  └─ tts    (tts.text_len / tts.text / tts.synth_ms / [wasted / waste.reason])
```
prompt/completion 按 OTel GenAI 语义约定用 `gen_ai.*` 记录，将来接 Langfuse 零改动。

### 「被浪费的算力」可见化（wasted / 截断）
ASR/LLM/TTS 三段统一口径：产物「做了但没被用上」时在**自身 span** 打 `wasted=true` + `waste.reason`，可在 Jaeger 直接按标签筛出被浪费的环节。三态含义：
- `abort`：被用户打断丢弃（ASR 识别完恰逢打断态 / LLM 流式生成中途 `client_abort` / TTS 合成完瞬间已打断）。
- `stale`：旧轮残留被跳过（TTS 合成句的 `current_sentence_id` 已落后于 `conn.sentence_id`）。
- `empty`：空识别（ASR 识别为空，本轮不进入 LLM/TTS）。
turn 级另记 `turn.end_reason`（completed/aborted/empty/error）+ `turn.aborted`，把「整轮是否被截断」可视化；`turn.empty_asr` 标空识别轮。

**wasted span 的视觉区分（2026-07 追加）**：Jaeger 时间轴的 span 颜色只按 **service 名**分配、无法按 tag 自动染色，故 `mark_wasted` 除打 `wasted=true`+`waste.reason` 外，再用 `span.update_name()` 把 operation name 追加「`·wasted:<reason>`」后缀（`tts`→`tts·wasted:abort`、`llm`→`llm·wasted:stale`），让被浪费的环节在时间轴/span 列表里直接肉眼可辨。选型对比：
- **操作名追加标记（采用）**：语义不失真、常驻可见、零 UI 依赖；代价是 `tts`/`llm` 的 operation 聚合会按新名字拆分。
- uiFind 搜索高亮（放弃）：`?uiFind=wasted` 只在打开某条 trace 时临时高亮，不常驻、需手动搜。
- span status=ERROR 标红（放弃）：会污染错误率指标、把「正常但没用上」谎称为错误。
实现幂等：name 已含 `·wasted:` 则不重复追加；`update_name` 包 try/except，异常不影响 wasted 标签。


## 开关（环境变量驱动，非 config.yaml）
> 智控台部署下 config 由 Java API 重建、会丢弃本地 YAML 自定义键，故走 env。
```
TELEMETRY_ENABLED=true                          # 总开关，默认关
OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318  # OTLP/HTTP，容器内走服务名
OTEL_SERVICE_NAME=xiaozhi-server
TELEMETRY_CAPTURE_CONTENT=true                  # 记录 prompt/completion 原文；含隐私可关
```
已在 `docker-compose.override.yml` 的 server 服务里默认注入 `TELEMETRY_ENABLED=true`。

## 代码改动清单
- 新增 `core/utils/telemetry.py`：env 驱动初始化 + turn/child span 封装 + `gen_ai.*` 记录，全部 try/except 降级。新增 `mark_wasted`（wasted+waste.reason）、`record_user_input`、LLM usage 的 thread-local 桥接（`stash_llm_usage`/`pop_llm_usage`）。
- `app.py`：`main()` 加载 config 后调 `setup_tracing()`。
- `core/providers/asr/base.py:handle_voice_stop`：开 turn span；asr 子 span 改**手动 enter/exit**（需拿到 `text_len` 后再决定 wasted），记 `asr.text`；空识别标 `asr.wasted=empty`、识别成功但恰逢打断标 `asr.wasted=abort`；空识别/异常路径主动 `end_turn_span`、并兜底关闭 asr span 防泄漏。
- `core/connection.py:chat()`：包 llm 子 span，记 dialogue（含 system prompt）、首 token TTFB、耗时、completion；流式中途 `client_abort` 标 `llm.wasted=abort`；收尾读 thread-local usage 记 `gen_ai.usage.prompt_tokens/completion_tokens/cached_tokens/cache_hit_rate`；`record_user_input`（depth==0）记 `user.input`；三条 return 路径均关闭 span。
- `core/connection.py:clearSpeakStatus(turn_end_reason=...)`：轮次结束关闭 turn span，传 end_reason（completed/aborted）。
- `core/handle/abortHandle.py`：打断走 `clearSpeakStatus(turn_end_reason="aborted")`。
- `core/providers/tts/base.py:to_tts_stream()`：包 tts 子 span，记合成耗时 + `tts.text`；合成完瞬间快照标 `tts.wasted`（abort/stale）。
- `core/providers/llm/openai/openai.py`：`response()`/`response_with_functions()` 加 `stream_options={"include_usage": True}`，从末尾 chunk 抽 `cached_tokens`（兼容 `prompt_tokens_details.cached_tokens` 新式与 `prompt_cache_hit_tokens` 旧式）经 thread-local 回传。
- `requirements.txt`：加 `opentelemetry-sdk==1.29.0`、`opentelemetry-exporter-otlp-proto-http==1.29.0`。

## 关键设计决策
- **OTLP/HTTP 而非 gRPC**：避免 `grpcio` 重型 native wheel（ECS 公网装大 wheel 极不稳，见部署踩坑）。
- **跨线程 context 显式传播**：ASR 在事件循环、`chat()` 在线程池、TTS 在守护线程；OTel 隐式 context 不跨线程，故 turn 的 parent context 必须显式挂在 conn 上手动继承。
- **turn context 注册表（2026-07 重构，替代单可变槽位）**：早期用 conn 上「单个可变槽位」`_otel_turn_ctx` 存当前轮 ctx，所有子 span 在「创建那一刻」读它。真机暴露三类严重 bug：(1) llm/tts 跑在长驻守护线程、与 turn 边界严重错位，读到的是「读那一刻」的当前轮而非自己所属轮 → 两轮塞进一个 turn、turn 时长跨 100+ 秒；(2) 上一轮 end 后不清 ctx，旧 ctx 一直向后污染；(3) `start_turn_span` 无显式 context，共享事件循环会**继承别的连接遗留的 implicit span** → **多个 session 混进同一 trace**。
  - 修复：改为 conn 上「按 `turn_key`(=业务不变量 `sentence_id`) 索引的注册表」`conn._otel_turns`（`OrderedDict` + 锁）。子 span 用**自己手里的 sentence_id** 显式查父（`child_span(..., turn_key=)`），绝不读全局可变槽位，从根本上消除错挂。
  - **root Context 强隔离**：turn span 强制用 `context=Context()`（全新 root）创建，绝不继承任何 implicit context，故每轮是独立 trace root、不同 session/不同轮天然隔离，杜绝跨连接混轮。
  - **pending turn**：ASR 起点建 turn 时 `conn.sentence_id` 还是上一轮旧值，新 sentence_id 在 `chat()` depth==0 才生成，故先建「pending turn」（不绑 key），`bind_turn(conn, sentence_id)` 时迁入注册表。ASR 阶段的 asr 子 span 用 `turn_key=None` 回退到 pending。
  - **系统主动发起链路兜底（2026-07 追加修复）**：空闲告别（`receiveAudioHandle.no_voice_close_connect` → `startToChat(依依不舍话术)`）、超字数提示等**绕过 ASR** 的链路直接进 `chat()`，没有 pending turn。若 `bind_turn` 只在有 pending 时迁移，本轮 llm/tts 会 `child_span(turn_key=)` 查不到父 → 沦为**分散在不同 trace 的孤儿**（真机实测：告别 tts 与其 llm 各自 root、彼此不同 trace，从 tts 完全找不到对应 LLM）。修复：`bind_turn` 无 pending 时**兜底 `_new_turn_record` 新建 turn** 再迁入，保证任何进 `chat()` 的轮都有 turn 父。
  - **孤儿修复 = 保留 ctx（不清）+ LRU**：`end_turn_span` 只 `span.end()`、保留记录在注册表，turn end 后其 `SpanContext` 仍不可变有效（OTel 规范允许父先于子结束），拖尾 tts/llm 用同一 turn_key 仍挂到本轮。注册表带上限（`_MAX_TURNS_PER_CONN=8`）LRU 清理防泄漏。**未采用纯引用计数**：流式 N 未知，句间归零会导致提前收尾+泄漏。
- **LLM usage 用 thread-local 桥接**：`self._llm` 是跨所有连接的**共享单例**、无 conn 引用，不能挂对象属性（并发串写）；但其流式生成器在与 `chat()` 相同的 executor 线程内迭代，故用 `threading.local()` 同线程回传 usage。
- **wasted 三态统一口径**：`abort/stale/empty` 标到各段**自身 span**，Jaeger 可直接按 `wasted=true` 筛被浪费环节；turn 级 `end_reason/aborted` 把整轮截断可视化。
- **wasted 快照精度约束**：TTS 的 `client_abort` 在下一轮 `receiveAudioHandle.py` 被重置为 False，故 wasted 判定必须在**合成完成的瞬间**取快照，不能事后回读；turn 级 `aborted` 作兜底。
- **绝不拖垮主链路**：OTel 未装/未开/任何异常 → no-op；`child_span` 禁用态 yield `None`，调用方判空。
- **属性截断**：单属性上限 8KB，超长 prompt 不撑爆 span。


## 上线步骤（已在 ECS 上线并端到端验证通过）
1. override 在 ECS `/srv/xiaozhi-server/docker-compose.override.yml`（项目根配置，含 jaeger 服务 + server 的 env + `image: server_local` 安全阀，见踩坑）。它不在 git repo 里，改后 `deploy/deploy.sh up` 或 force-recreate 生效。
2. 代码同步 → `deploy/pull.sh`（server `git pull` + restart；`core/` 经 src 软链自动更新，`telemetry.py` 随之生效）。
3. 依赖变更 → `deploy/rebuild-server.sh`（补装 2 个 otel 包，生成 `server_local`；脚本第 4 步已顺带 `compose up` 拉起 jaeger 并 force-recreate server）。
4. 验证结论：容器内 `import opentelemetry` 通过；启动日志出现 `[telemetry] enabled`；VAD/ASR/WS 正常；Jaeger 已注册 `xiaozhi-server` service，落库一条 `conversation.turn→asr/llm/tts` 完整 trace，`gen_ai.*` prompt/completion/首token 均正确存储。

## 访问 Jaeger UI（用 SSH 隧道，安全组不放行 16686）
Jaeger all-in-one **零鉴权**，且 `TELEMETRY_CAPTURE_CONTENT=true` 时存的是**真实对话 prompt/回答原文**，故：
- **禁止 `0.0.0.0/0`**：等于把用户对话内容对全网无鉴权裸露，违背项目 `/32` 白名单安全原则。
- **禁止靠单 IP `/32`**：办公网出口 IP 会变，维护成本高。
- **推荐 SSH 本地转发**（16686/4318 一律不放行公网，走加密 SSH，换 IP 无感）：
  ```bash
  ssh -N -L 16686:localhost:16686 root@118.196.120.182
  # 浏览器开 http://localhost:16686，Service 选 xiaozhi-server
  ```
- 若必须浏览器公网直连：走 Nginx 反代 + Basic Auth 再放行端口，不可裸露。
- `4318`（OTLP 采集）仅容器内网用，永不放行公网。

## 自测结论（本地已验证通过）
用 `InMemorySpanExporter` 直接驱动真实 `telemetry.py`，多线程模拟真实时序（llm/tts 放独立线程跑），`deploy/telemetry_selftest.py` **29/29 通过**（隔离 venv 跑：`/tmp/otel_venv/bin/python deploy/telemetry_selftest.py`）：
- **孤儿修复**：turn `end()` 后另起线程产出拖尾 tts，asr/llm/tts 3 个子 span 仍全部挂在原 turn 下（parent 非空）→ **跨线程传播 + 保留 ctx 成立**；且 turn 自身 parent 为空（独立 trace root）。
- **★跨轮交错（场景5，复现「时序混乱」bug）**：turn-A 完整 end 后 turn-B 起，A 的迟到 tts 与 B 的 tts 并发到达——A 的 tts 用 `turn_key=A` 精确挂回 A、B 的挂 B，两轮分属两个独立 trace_id，**不再合并成一个巨型 turn**。
- **★跨 session 隔离（场景6，复现「混合多个 sessionid」bug）**：两连接用 `Barrier` 强制同刻交错并发，各自 turn 的 trace_id 不同，每个会话的 asr/llm/tts 子 span 只挂在本会话 turn 下、trace_id 与本会话一致，**绝无跨会话串扰**。
- **★系统主动发起、绕过 ASR（场景7，复现「找不到LLM的孤儿tts」bug）**：空闲告别不走 ASR、无 pending turn，`bind_turn` 兜底新建 turn 后，llm/tts 全部挂在该 turn 下且同一 trace，**从 turn 能找到对应 LLM，不再是分散孤儿**。
- **wasted 三态**：llm abort、tts abort、tts stale、asr empty、asr abort 均正确落 `wasted=true` + `waste.reason`；stale tts 仍用 turn_key 挂在其真正所属轮下（非孤儿/非错轮）。被 wasted 的 span operation name 正确改为 `llm·wasted:abort` / `tts·wasted:abort` 形态（供 Jaeger 时间轴肉眼可辨）。
- **turn 语义**：`turn.end_reason` = completed/aborted/empty、`turn.aborted`、`turn.empty_asr` 全部正确。
- **cached_tokens thread-local 桥接**：并发线程 stash/pop 不互相串写，`gen_ai.usage.cached_tokens` 正确回传。
- **pending turn**：ASR 阶段（尚未 bind）asr 子 span 用 `turn_key=None` 正确挂在 pending turn 下；`bind_turn` 后迁入注册表、后续 llm/tts 用 sentence_id 精确挂父。
- **禁用态**：`setup_tracing()` 返回 False，所有 API no-op、`start_turn_span` 返回 None、`child_span` 返回 None、`bind_turn`/`mark_wasted`/`record_user_input` 无异常。
- 改动文件 `py_compile` 全通过；OTLP/HTTP exporter 在 py3.9 venv 导入正常。
- **ECS 真机端到端**：`turn.end_reason=completed`、`user.input`、`gen_ai.usage.cache_hit_rate≈0.88`、`cached_tokens` 正常命中、`tts.text` 全部落库，**ORPHANS: NONE**。

## 踩坑结论
- **override 必须保留 `image: server_local` 行**：override 的 server 段显式写 `image: xiaozhi-esp32-server:server_local`，让容器跑 rebuild 产出的本地镜像；同时作为安全阀，避免误改到 jaeger 的 image 行。当前 `rebuild-server.sh` 不再 sed 改 image（override 已硬编码），直接 build + force-recreate。
- **override 不在 git repo**：override 是 server 项目根配置（`/srv/xiaozhi-server/docker-compose.override.yml`），不随 `git pull` 更新；改了 override 直接在 ECS 编辑，再 `deploy/deploy.sh up` 或 force-recreate 生效。版本化参考副本在仓库 `deploy/docker-compose.override.yml`。
- **jaeger 与 server 必须同网络**：base compose 用 `default` 网络，jaeger 也显式挂 `default`，否则 server 内 `jaeger:4318` 解析不到。
- **生产降级已验证**：旧镜像（未装 otel）+ 开关未开时 server 正常启动——`setup_tracing()` 开关关闭时直接 return、不 import otel，import 失败也降级 no-op，主链路零影响。
- **all-in-one 内存存储**：Jaeger 重启即清空历史 trace（含冒烟测试数据）。需长期留存要换带持久化后端（Badger/ES），当前用于实时观测足够。
- **wasted 判定要取合成瞬间快照**：`conn.client_abort` 会在下一轮 `receiveAudioHandle.py` 被重置为 False，事后回读会漏判；TTS 在合成完成瞬间取快照，turn 级 aborted 兜底。


## Jaeger 只适合抽查，不做评估
Jaeger 能按 traceID/时间/service 看时延链路、点开某轮看 prompt/回答原文；但**不能按 prompt 内容检索、不做质量打分、无脱敏**。系统性调 prompt/评估回答质量时，用同一套 `gen_ai.*` span 分流一份到 **Langfuse（自托管）**，exporter 不变。

## 语音评测平台演进（北极星）
目标：xiaozhi-server → voice ASR 评测平台（+ speech2speech 数据集），Phoenix 为统一 Web 底座。
- 功能分两类：**A trace 预览**（OTel gen_ai event 实时进 Phoenix，按时序看音频→ASR→LLM）；**B 评测**（权威 JSONL→Phoenix Dataset，jiwer 算 WER/CER）。两类共享同一份 WAV，音频只用 path/URL、禁 base64。
- Phoenix vs Jaeger：同吃 OTLP:4318，区别在渲染——Jaeger 把 gen_ai 当普通属性平铺，Phoenix 原生渲染对话+可播音频+评测层。长期收敛到 Phoenix 一套，endpoint 切换即迁移、零埋点改动。
- JSONL 规范对齐 Claude Code / Codex：首行 Codex 风 `session_meta`，消息体 Claude content-block，`input_audio.path` 引用 WAV，`sentenceId` 为关联键，`turnSource` 区分 asr/system。落盘只有一份权威格式，定制化全收敛到 adapter 层。

### JSONL 落盘规范（core/utils/voice_session_log.py）
一 session 一文件，append-only，一行一 JSON。三种行：
- `session_meta`（首行，Codex 风）：`payload.{sessionId,deviceId,originator,audio{format,sampleRate,channels}}`。
- `user`（Claude content-block）：`sentenceId` + `turnSource`(asr/system)；`asr` 轮 content 含 `input_audio` 块，`source.path` 为相对 jsonl 目录的 WAV 相对路径 + `transcript`；`system` 轮 content 为空数组、无音频。
- `assistant`：同 `sentenceId`，`parentUuid` 指向本轮 user turn；content 为 text 块 + 可选 `message.model`/`message.usage` + 可选 `metrics.durationMs`。
公共字段：`type/uuid/parentUuid/timestamp(ISO8601 UTC 毫秒)/sessionId`。

路径规范：
```
<VOICE_DATASET_DIR>/<device_id>/<session_id>.jsonl                   # rollout
<VOICE_DATASET_DIR>/<device_id>/<session_id>/audio/<sentence_id>.wav # 音频 int16/16k/mono
```
device_id 含 ':' 等经 _sanitize 转路径安全片段；WAV 只在 `turnSource==asr` 有 pending PCM 时落。

开关（env，未开启全链路 no-op）：
```
VOICE_DATASET_ENABLED=true            # 总开关，默认关
VOICE_DATASET_DIR=data/voice_sessions # 数据根目录
VOICE_DATASET_CAPTURE_TEXT=true       # 记录 transcript/completion 原文
```

埋点接入点：
- `app.py` main() 调 `setup_voice_log()`（紧随 setup_tracing）。
- `asr/base.py handle_voice_stop`：text_len>0 分支 `stash_user_audio(conn, combined_pcm_data, asr_text, 16000)`（此刻已 16k/mono/int16，sentence_id 尚未生成，故 pending 暂存）。
- `connection.py chat() depth==0`：bind_turn 后 `flush_user_turn(conn, sentence_id, turn_source=asr|system)`（有 _voice_pending=asr）。
- `connection.py` LLM completion 收尾：`response_message` 非空时 `record_assistant_turn(...)`，避免纯 tool_call 轮重复（递归轮共享同一 sentence_id）。

### 消费端与 adapter（tools/voice_dataset_to_phoenix.py）
落盘不为任何单一消费端定制，定制全收敛到 adapter 层：
- 人：直接 jq/less 裸读。
- Phoenix：adapter 解析 JSONL→Dataset，音频转本地绝对路径或 `--audio-url-base` 拼 URL（禁 base64）。
- jiwer：adapter `compute_wer(ref,hyp)` 挂点，返回 {wer,cer}，reference 待人工标注补齐。
- speech2speech：按 sentenceId 取 (WAV, 文本) 对。
adapter 特性：phoenix/jiwer/pandas 全 try-import 可选降级；仅收 `turnSource==asr` 且含 input_audio 的轮为评测样本；system 轮自动过滤。已 mock 端到端验证（解析/过滤/音频路径/usage 对齐正确）。

### 旧持久化链路治理结论（Task#7）
| 链路 | 服务对象 | 与JSONL重叠 | 处置 |
|---|---|---|---|
| Python 上报 report→manage_report | 智控台产品 | 部分（音频） | 降级：chat_history_conf 默认设 1（仅文本），音频卸给 JSONL |
| asr/base.py save_audio_to_file | ASR 推理中间态 | 无 | 保留原样，勿动 |
| Java ai_agent_chat_audio(LONGBLOB) | 产品回放 | 音频冗余 | 共存，链路降级后停止增长、可后续归档 |
| manager-web 回放按钮 | 产品 | 无 | 随上报链路联动，关音频则隐藏 |
| Jaeger all-in-one | 时延观测 | 无 | 收敛到 Phoenix（同吃 4318，endpoint 切换即迁移） |
唯一音频职责重叠是 `ai_agent_chat_audio`；核心权衡：`chat_history_conf` 降为 1 卸 DB 音频存储，代价是智控台 Web 回放失效——**需产品确认后再动**。

### 本地 Phoenix 验证结论（Task#5，pip 方案，绕开 Docker）
本机 Docker Desktop 反复起不来（backend 拉起即崩、socket 建不起），故本地验证改用 **pip 版 Phoenix**（`import phoenix as px; px.launch_app()`，SQLite 后端，:6006），Docker 仅 ECS 上线才需要。隔离 venv `/tmp/phoenix_venv` 已保留复用。实测：
- **Python 3.9 无硬限制**：`arize-phoenix` 最新 12.15.1 在 Python 3.9.6 直接装成功并可 import（依赖 pandas 2.3.3 / sqlean 等），无需 pyenv 装 3.10。仅 LibreSSL urllib3 无害告警。
- **端到端通**：构造 3 条真实 WAV（16k/mono/int16/1s）mock → adapter 解析 3 条 → `upload_dataset` 成功进 Phoenix Dataset `voice_asr_demo`（examples=3，`/datasets/.../examples` 可见）。
- **内存**：Phoenix server 常驻 RSS **约 278MB**（含 pandas/pyarrow/sqlalchemy），4C/8G ECS 可容。
- **★中文 WER 的坑**：`compute_wer("今天天气怎么样样","今天天气怎么样")` 返回 `{wer:1.0, cer:0.125}`。jiwer 默认按空格分词，中文整句=1 token 故 wer 失真为 1.0；**中文场景应以 CER 为主，或对 reference/hypothesis 先分词再算 WER**。这是 adapter reference 接入前必须处理的问题。

## 关键行动日志（每条≤40字）
- 建 voice_session_log.py：权威 JSONL+WAV 落盘，env 驱动、no-op 降级。
- 落盘路径 <ROOT>/<device_id>/<session_id>.jsonl + audio/<sentence_id>.wav。
- app.py setup_voice_log()、asr/base.py stash PCM、connection.py flush user turn 接入。
- connection.py 补 assistant turn 记录点，parentUuid 串本轮 sentence_id。
- 四文件 py_compile 全通过，Task#3 埋点接入收尾完成。
- 结论：JSONL 消费端=人/Phoenix/jiwer/s2s，落盘不定制、adapter 层定制。
- 结论：Claude/Codex 官方 CLI 不消费本 JSONL，对齐红利=不造字段+范式复用+人眼熟。
- 追加 Task#7 治理：审旧持久化链路（manage_report/save_audio_to_file/Java LONGBLOB）保留or降级。
- 追加治理项：Jaeger→Phoenix 追踪后端收敛为一套，省 ECS 双份运维。
- 建 tools/voice_dataset_to_phoenix.py adapter，mock 端到端验证通过。
- 本机 Docker 起不来，本地验证改用 pip 版 Phoenix（px.launch_app）。
- 结论：Phoenix 12.15.1 在 py3.9.6 直接可用，无需升 3.10。
- 验证：3 条 mock 经 adapter 入 Phoenix Dataset，examples=3，RSS≈278MB。
- 坑：jiwer 按空格分词，中文 WER 失真，中文以 CER 为主或先分词。
- Task#7 治理降级为 P2 todo，暂不执行，待产品确认 chat_history_conf。
- 结论：Phoenix 无 Jaeger 染色缺陷，按 attributes["wasted"] 直接筛。
- 提醒：·wasted 后缀污染 Phoenix 聚合，迁 Phoenix 时应条件化/去掉。
- 上线：override 加 VOICE_DATASET_* env + /data/voice_sessions 数据盘卷。
- 上线：force-recreate 生效(restart 不重读 compose)，env+卷已核验。
- 上线验证：日志 [voice_log] enabled dir=/data/voice_sessions，ASR/WS 正常。

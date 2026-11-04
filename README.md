# ModelBench

在 PC 本机运行的编码会话压测工具：独立轻量前端、aiohttp 后端、SQLite 历史记录，以及单独运行的异步发流进程。无需登录，无 Node 构建或外部数据库；只依赖 Python 3.11+ 和 aiohttp。

当前实现是本地测试工具，不是 ModelPilot 的 Benchmark 编排控制面；不部署服务，不修改集群资源，也不接入 Portal。

## 启动

PowerShell：

```powershell
cd D:\Code\ModelPilot\ModelBench
uv sync
uv run python server.py
```

打开 <http://127.0.0.1:8765>。首次使用先填服务地址与模型名称，点击「预览请求体」检查负载；只有点击「启动压测」才会发流。

也可直接使用已安装的虚拟环境：

```powershell
.\.venv\Scripts\python.exe server.py
```

使用 pip 时：`python -m pip install "aiohttp>=3.11,<4"`，然后 `python server.py`。

服务仅监听 `127.0.0.1`，不需要鉴权。默认端口 8765，可通过 `--port 8766` 修改；`--data <目录>` 指定 SQLite 与报告位置。静态前端在 `frontend/`，与 API 同源托管，不依赖 CDN。

若目标推理入口需要凭证，在启动本地服务前设置 `$env:OPENAI_API_KEY`；这是目标服务的可选凭证，与本工具登录无关。也可在界面的 API Key 密码框直接输入密钥（不含 Bearer 前缀），优先于环境变量。密钥仅通过子进程环境传递，不写入 SQLite、报告、日志或命令行参数；启动成功后输入框清空，复用历史配置时需重新输入。留空则使用环境变量，无凭证时不发送 Authorization。请求不自动读取系统代理设置。

## 负载构造

- 最多 2000 个异步在途请求；每个虚拟会话依次执行多轮，实际回答与工具调用追加到下一轮上下文。
- 内置 `claude-code`、`codex`、`opencode`、`deepseek` 四种**合成、Chat Completions 规范化工具画像**。包括工具 schema、项目约定、长代码块、历史 assistant/tool 消息和不断增长的工具结果。
- 工具输出是可复现的合成代码，**不会执行模型返回的命令**；它不保证工具语义正确或真实编程任务能完成。模型也不保证每轮调用工具。
- `project` 按项目共享长前缀；`unique` 每会话独立；`shared` 同画像共享。差异放在长上下文之前，避免只修改末尾而仍然命中整个前缀。项目与画像组合决定实际前缀，不能简单把项目数等同于实际唯一前缀数。
- 字符数不是 token 数。输入来自 ASCII 代码生成器，实际 token 长度以目标模型及 usage 为准；不会强制填充到精确 token 长度，也没有自动压缩上下文。上下文窗口必须容纳初始内容、每轮增长及输出。
- 同一 `seed` 可重复生成相同前缀，跨次运行可能复用服务端缓存。冷缓存实验请另用 seed，或由操作者在服务端清理缓存。
- 这是闭环用户模拟：并发是上限，不是固定 QPS；思考时间、升压、错误与会话结束都会降低实际并发。错误时终止该会话，不自动重试掩盖过载。
- 默认不发送 session 头。按实际路由契约填写请求头后，`stable` 同会话稳定、`rotate` 每轮换值；稳定 ID 不代表服务端一定启用了亲和策略。
- 连接池在用户之间共享，不提供每用户独享连接。`fresh_connections` 关闭复用，可用于连接粘滞对照。

### 协议与高精度边界

目前只发 `/v1/chat/completions`，不支持 legacy `/v1/completions`、原生 Anthropic Messages、Responses 或 WebSocket。Claude Code 与 Codex 的原生协议和工具定义与合成画像不同；OpenCode 的实际请求也依赖 provider 与版本。DeepSeek 在这里表示使用其 Chat Completions/tool-call 约定的通用编码代理，不代表某个唯一官方 harness。

高精度实验应使用**脱敏后的真实请求回放**，不要把合成画像当成客户端逐字节复刻。CLI 支持 JSONL：每行一个会话，`requests` 是顺序排列的完整 Chat Completions 请求体。

```json
{"requests":[{"messages":[{"role":"user","content":"Inspect this code"}],"max_tokens":128},{"messages":[{"role":"user","content":"Inspect this code"},{"role":"assistant","content":"Recorded answer"},{"role":"user","content":"Add a regression test"}],"max_tokens":128}]}
```

```powershell
uv run python bench.py --model my-model --base-url http://localhost:8000/v1 --replay sessions.local.jsonl --sessions 100 --concurrency 20
```

回放保留每轮请求体及工具 schema，但覆盖 `model`、`stream` 和 usage 开关；使用记录中的下一轮，不注入本次模型答案。会话数超过文件行数时循环复用，可能提高缓存复用，实验时要注意。回放目前仅在 CLI 提供，前端专注合成负载。

## 推荐实验矩阵

1. 相同参数分别运行并发 32 / 128 / 512 / 1000 / 2000；观察实际并发峰值、客户端事件循环延迟、错误率与 TTFT P95/P99。
2. 对比 `shared / project / unique`，在各次实验中控制 seed 和缓存冷热状态。
3. 保持前缀一致，对比不发送 session、稳定 session、每轮轮换 session。
4. 通过「后端标识响应头」读取真实的 serving backend 身份，检查同会话相邻轮次是否落在同一后端。代理节点标识不能替代实际推理实例标识。
5. 按轮次对比 TTFT 与 usage；不要把运行顺序、连接复用或系统升温造成的变化归因于缓存策略。

2000 并发只是配置能力，**未经过真实集群吞吐验证**。高并发长上下文可能先耗尽 PC 内存、文件描述符或 CPU。单进程 JSON 构造、摘要和结果落盘也有开销；事件循环延迟显著上升时结果会受到客户端影响。建议先从小规模开始，检查实际 prompt tokens 与上下文上限，再由操作者升压。

## 指标口径

| 指标 | 定义与限制 |
|---|---|
| TTFT | 从发起 HTTP 请求到收到首个非空 content、reasoning_content 或工具名称/参数片段；忽略仅 role 的事件。包含客户端编码、网络和服务排队。 |
| TPOT | `(最后输出块时间 - 首输出块时间) / (completion_tokens - 1)`；依赖服务端 token usage，至少两个输出块、两个输出 token，否则留空。SSE 合并 token、隐藏 reasoning 会造成近似误差。 |
| SSE 块间隔 | 非空输出块间隔的每请求平均值；不是 token ITL。报告中的 P95 是请求平均块间隔的分位数。 |
| E2E | 请求开始到读到 DONE 或异常。要求 finish_reason 和 DONE；不完整流记失败。 |
| KV 命中 | `usage.prompt_tokens_details.cached_tokens` 或 `usage.prompt_cache_hit_tokens`。没有就为 null；服务端计费缓存不一定等于物理 KV 块复用。 |
| 加权 KV 命中率 | 仅在成功且同时报告 prompt/cache token 的请求中，`sum(cached)/sum(prompt)`；同时提供 usage 覆盖率。 |
| TPOT/TTFT 分布 | 成功请求的 P50/P95/P99；失败单独统计。原始 JSONL 保留失败请求已观测到的指标。 |
| 后端同节点比例 | 同会话相邻两轮均成功且都有 backend 响应头时，比较是否相同；没有有效样本则 null。 |
| 输出吞吐 | 成功请求已报告的 completion tokens / 完整运行时长；缺失 usage 不估算，需结合覆盖率。 |

升压和思考时间包含在整体运行时长中。`client_request_id` 只是本地记录标识，不冒充 ModelFlow 生成的 `modelpilot.request.id`。服务器 `x-request-id` 单独保留。

## 结果与生命周期

默认目录 `data/`：

```text
modelbench.sqlite3       # 配置、状态、最终汇总
<run UUID>/
  config.json
  progress.json         # 运行中约每 0.5 秒更新
  requests.jsonl        # 逐请求指标，不保存提示词或输出文本
  summary.json          # 汇总、按画像/轮次分组
  worker.log
```

一次只运行一个任务，防止多次发流互相干扰。点击停止会取消在途请求，保存已完成请求的部分报告；被取消的在途请求不计入完成请求数。服务正常关闭会停止子进程。异常退出/断电后，SQLite 中运行中记录会在下次启动标记为 interrupted；已写入的原始指标文件仍保留，但不自动恢复负载。历史列表展示最近 100 个任务，旧记录仍保存在 SQLite。

命令行也可独立运行：

```powershell
uv run python bench.py --model my-model --base-url http://localhost:8000/v1 --concurrency 2000 --sessions 4000 --turns 6 --projects 200 --ramp-seconds 30
uv run python bench.py --model my-model --dry-run
```

CLI 默认输出到 `results/`，不写 SQLite。dry-run 不联网，写一个完整样例请求；若使用回放，样例可能含真实内容，应先脱敏。

## 代码测试

```powershell
uv sync --group dev
uv run python -m pytest -q
node --check frontend/app.js  # 可选，只有检查 JS 时需要 Node
```

测试只使用本机模拟 SSE 服务，覆盖分片解析、多行事件、工具参数拼接、断流/HTTP 错误、指标缺失、前缀差异、真实子进程启动/停止、并发上限、配置校验、SQLite 重启和报告下载。不会连接真实推理集群。

## 开源基线调研

2026-11-04 阅读上游 README/文档，未复制上游代码，也未将浮动 main 作为运行依赖：

- [inference-perf](https://github.com/kubernetes-sigs/inference-perf)：最匹配。已有 shared-prefix、multi-turn、[synthetic agentic](https://github.com/kubernetes-sigs/inference-perf/blob/main/docs/synthetic_agentic.md)、工具循环、上下文增长、真实 trace 回放和多进程发流。适合作为后续大规模/分布式执行基线。
- [AIPerf](https://github.com/ai-dynamo/aiperf)：多进程性能测量、并发/QPS 模式、trace 回放、详细报告。
- [GuideLLM](https://github.com/vllm-project/guidellm)：SLO、并发扫描、通用数据集和报表。
- [Codex client 实现](https://github.com/openai/codex/blob/main/codex-rs/core/src/client.rs)：确认其 Responses/WebSocket 与会话/turn 状态机制，不能将普通 Chat Completions 画像称为原生 Codex 请求。

本版本选择轻量独立实现，以便 PC 本地使用和调节会话/前缀实验变量。后续高精度优先引入真实脱敏 trace，再增加原生协议适配与独立 tokenizer 长度校验。

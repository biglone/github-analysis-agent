# GitHub Analysis Agent — 第一、二阶段

这是学习 AI Agent 开发的项目，按 [PLAN.md](PLAN.md) 实现 GitHub 工具层、独立的 DeepSeek 模型适配层和最小只读 Agent Runtime。当前不包含 RAG、长期记忆或 Agent 框架。

## 安装与运行

需要 Python 3.11+。在仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
python examples/inspect_repository.py biglone github-analysis-agent --file PLAN.md
python examples/analyze_repository.py biglone github-analysis-agent
```

Windows PowerShell 的激活命令为 `.venv\Scripts\Activate.ps1`。

公开仓库可以匿名读取。程序从环境变量 `GITHUB_TOKEN` 读取凭证。可以复制 `.env.example` 为 `.env` 后填写 GitHub Token；`.env` 会自动加载，且已加入 Git 忽略规则。GitHub 连接器授权与此 Python 程序的凭证相互独立。私有仓库的细粒度 Token 需要该仓库的 Contents: read 权限。不把 Token 写入源码或提交。

如果使用 SOCKS 代理（例如 Clash），额外安装 `python -m pip install -e '.[proxy]'`。HTTPX 默认读取环境中的代理配置。

## DeepSeek 模型适配层（任务 2.1）

适配层通过 OpenAI 兼容 SDK 请求 DeepSeek 官方 Chat Completions API。安装项目依赖后，在项目根目录复制示例配置并编辑：

```bash
cp .env.example .env
```

在 `.env` 中填入从 DeepSeek Platform 创建的 Key，并保留默认模型或自行更改：

```dotenv
DEEPSEEK_API_KEY=你的真实API密钥
DEEPSEEK_MODEL=deepseek-flash
```

程序初始化 `DeepSeekAdapter` 时会自动加载项目目录中的 `.env`。系统已设置的环境变量优先于 `.env`。`.env` 已被 Git 忽略；提交前可用 `git check-ignore .env` 确认。不要把真实 Key 写入 `.env.example` 或提交到仓库。

```python
from github_analysis_agent import DeepSeekAdapter

adapter = DeepSeekAdapter(timeout=30)
reply = adapter.complete([{"role": "user", "content": "请介绍这个仓库。"}])
print(reply.text)
print(reply.usage.total_tokens)
```

默认请求会附上 `list_directory` 和 `read_file` 的 Function Calling 定义。模型返回的调用以 `reply.tool_calls` 中的名称、参数和调用 ID 提供；适配层不会导入或执行 GitHub 工具，也不负责多轮对话。设置 `enable_tools=False` 可只请求文本响应。错误类型见 `github_analysis_agent.model_errors`；异常消息不包含 SDK 响应正文或 API Key。

## Agent Runtime（任务 2.2）

Runtime 将模型适配层与只读 GitHub 工具连接，维护多轮消息并将每个工具结果按 `tool_call_id` 回传。仅注册 `list_directory`、`read_file`；目标仓库固定为 `run` 指定的 owner/repo，不允许模型访问其他仓库、执行 Python 或运行系统命令。

```bash
python examples/analyze_repository.py biglone github-analysis-agent
```

也可以在 Python 中控制资源上限：

```python
from github_analysis_agent import AgentRuntime

with AgentRuntime(max_turns=8, max_tool_calls=12,
                  timeout_seconds=120, max_tool_result_chars=20_000) as agent:
    result = agent.run(
        "分析项目目录结构，并说明主要文件的用途。",
        owner="biglone",
        repo="github-analysis-agent",
    )

print(result.answer)
print(result.termination_reason, result.token_usage.total_tokens)
```

`termination_reason` 为 `completed` 表示模型给出了最终回答；也可能是 `max_turns`、`max_tool_calls`、`timeout` 或 API/权限错误。404 等可恢复工具错误会以安全 JSON 结果发给模型；相同失败调用在本轮不会重复执行。超长工具结果不会发送给模型，而会变成明确错误。可通过 `result.tool_stats` 查看工具调用状态。Runtime 日志只记录模型调用数、工具名、状态、耗时、token 用量和结束原因，不记录任务、参数、文件路径、内容或凭证。示例将 HTTPX 请求日志保持在 WARNING；如果自行启用 HTTPX INFO 日志，HTTP 请求 URL 会包含仓库路径。

## 接口

```python
from github_analysis_agent import GitHubClient, list_directory, read_file

with GitHubClient(timeout=10.0) as client:
    entries = list_directory(client, "biglone", "github-analysis-agent")
    result = read_file(client, "biglone", "github-analysis-agent", "PLAN.md")
    print(result["text"])
```

- `list_directory(client, owner, repo, path="", ref=None)` 返回直接子项列表，每项包含 `name/path/type/size/sha`。
- `read_file(client, owner, repo, path, ref=None)` 返回包含 `path/sha/size/text` 的字典。
- `ref` 可指定分支、标签或提交；省略时使用默认分支。需要一致快照时，两次调用传同一提交 SHA。
- 根目录使用空字符串，其他路径为仓库相对路径；拒绝绝对路径、反斜杠、控制字符及空、`.`、`..` 路径段。逐段 URL 编码，ref 独立作为查询参数。
- 显式传入 `token=""` 可禁用环境 Token；客户端需通过 `with` 或 `close()` 释放连接。

## 设计与边界

`client.py` 管 HTTP、认证、超时、状态码和 JSON；`tools.py` 管目录与文件语义；`errors.py` 定义异常。这样请求成功但类型错误时，也能明确告知调用者；测试使用 `httpx.MockTransport`，不访问网络。

解码有两步：JSON 中的 Base64 字符串先还原字节，再将字节严格按 UTF-8 解码。中文使用多个 UTF-8 字节，所以文件大小按字节而非字符计算。空文件是合法结果，返回空文本；失败抛异常，不伪装成空结果。

文件最大 1,000,000 字节，检查声明大小、编码内容长度及解码后的实际大小。只支持内联 Base64 的普通 UTF-8 文本，拒绝 NUL 二进制内容、显式标识的符号链接与子模块，不访问 download_url 或外部目标。GitHub 可能将指向普通文件的符号链接透明解析为文件；仅靠 Contents API 无法识别这种未标识的情况。

目录不递归；特殊条目可列出并规范其 type，但不能用 read_file 读取。达到 1,000 项发出 `DirectoryLimitWarning`，结果可能不完整；第一阶段不使用 Trees API 补全。

## 错误与重试

异常均继承 `GitHubError`，来自 `github_analysis_agent.errors`：

| 情况 | 异常 |
| --- | --- |
| 非法参数 | InvalidArgumentError |
| 401 | AuthenticationError |
| 权限型 403 | PermissionDeniedError |
| 限流型 403 / 429 | RateLimitError |
| 普通 404 | NotFoundError（也可能是私有仓库不可访问） |
| API 明确报告空仓库 | EmptyRepositoryError |
| 超时/连接失败 | NetworkError |
| 5xx | ServiceError |
| JSON / 字段 / 大小不一致 | ResponseFormatError |
| 文件目录类型错误 | WrongContentTypeError |
| 超限/非 UTF-8/不支持条目或编码 | UnsupportedContentError |
| Base64 损坏 | ContentDecodeError |

HTTP 错误保留 `status_code/request_id/retry_after/rate_limit_reset`。网络、服务、限流错误标记 `retryable=True`，但不自动重试。默认超时 10 秒（HTTPX 各网络阶段的超时，不是总调用时间上限），不跟随重定向。日志仅记录路径、状态码和请求 ID，不记录 Token 或响应内容。

## 测试与验收

```bash
python -m pytest -q
```

测试覆盖根目录/子目录/空目录、1,000 项警告、类型错误、特殊条目、英文/中文/空文件、大小边界、Base64 换行与损坏、非 UTF-8、字段缺失、路径编码、ref、认证、超时、HTTP 与网络错误。测试无需联网或真实 Token。

真实 API 手动验收：运行示例，确认先列出 PLAN.md，再打印中文计划内容；退出码应为 0。示例的 `--file` 必须是 `--path` 所列目录中的普通文件完整路径，例如 `--path src/github_analysis_agent --file src/github_analysis_agent/client.py`。

复习检查：能解释 HTTP 请求 → JSON 响应 → Base64 解码成字节 → UTF-8 解码成文本，并区分 HTTP 错误与工具语义错误。

参考：[GitHub Contents API](https://docs.github.com/en/rest/repos/contents)、[DeepSeek API 模型与定价](https://api-docs.deepseek.com/quick_start/pricing/)、[DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/function_calling)、[OpenAI Python SDK](https://github.com/openai/openai-python)、[HTTPX](https://www.python-httpx.org/)、[pytest](https://docs.pytest.org/)。

### 本次验证记录（2026-10-14）

- Python 3.12、HTTPX 0.28.1、pytest 8.4.2：76 个测试通过。
- 真实 Contents API：运行示例列出本仓库根目录，然后读取 PLAN.md，中文内容正确还原，退出码 0。
- SOCKS 环境安装 proxy 可选依赖后完成上述真实验收。

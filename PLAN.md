**1. 阶段目标**

先做两个可以直接调用、独立测试的 Python 工具：

- `list_directory`：列出指定目录的直接子项。
- `read_file`：读取指定文本文件，将 Base64 内容解码为 UTF-8 文本。

这一阶段建立 Agent 的“工具层”。后续接入模型时，模型负责选择工具和参数，现有 Python 函数负责实际执行。暂时不接入模型、RAG、记忆或 Agent 框架，让你先理解完整的请求与返回流程。

**2. 推荐项目结构**

```text
github-analysis-agent/
├── README.md
├── pyproject.toml
├── .gitignore
├── .env.example
├── src/
│   └── github_analysis_agent/
│       ├── __init__.py
│       ├── client.py
│       ├── tools.py
│       └── errors.py
├── examples/
│   └── inspect_repository.py
└── tests/
    ├── test_client.py
    └── test_tools.py
```

| 文件 | 职责 |
| --- | --- |
| `client.py` | 统一处理 HTTP 请求、认证、超时和响应错误 |
| `tools.py` | 实现两个工具，校验返回类型、整理结果、解码文件 |
| `errors.py` | 定义可识别的异常 |
| `pyproject.toml` | 集中管理包信息、依赖和测试配置 |
| `examples/inspect_repository.py` | 展示“列目录 → 选择文件 → 读内容”的过程 |
| `README.md` | 安装、运行、接口说明及学习笔记 |

分开 `client` 和 `tools` 的原因是：**网络访问与工具语义是两件事**。例如请求成功了，但 `read_file` 收到目录，这仍然是工具使用错误。这个划分也便于测试时替换网络响应。

**3. 依赖选择**

建议使用 **Python 3.11+、同步调用、虚拟环境和 pip**。

| 类型 | 选择 | 原因 |
| --- | --- | --- |
| 运行依赖 | `httpx` | 直接使用 REST API，保留请求、状态码和 JSON 的学习过程 |
| 开发依赖 | `pytest` | 测试工具行为和异常分支 |
| HTTP 模拟 | `httpx.MockTransport` | 不额外引入模拟库；测试不消耗 GitHub 配额 |
| 标准库 | `base64`、`os` 等 | 解码、读取环境变量，无需额外安装 |

先不引入 PyGithub 或 LangChain，减少第一阶段需要理解的抽象层。

Token 从环境变量 `GITHUB_TOKEN` 读取；项目初始化 GitHub 或 DeepSeek 客户端时会自动加载项目目录中的 `.env`，并优先使用已经设置的系统环境变量。公开仓库可以匿名读取；需要 Token 时，由你在运行环境中配置，连接器的授权不会自动成为 Python 程序的凭证。私有仓库使用细粒度 Token 时需要对应仓库的 `Contents: read` 权限。[GitHub Docs](https://docs.github.com/en/rest/repos/contents?apiVersion=2026-03-10&utm_source=chatgpt.com)

**4. 两个工具的接口与边界**

建议由 `GitHubClient` 保存连接配置，工具显式接收它：

```python
list_directory(client, owner, repo, path="", ref=None)
read_file(client, owner, repo, path, ref=None)
```

两个工具都使用：

```text
GET /repos/{owner}/{repo}/contents/{path}
```

省略路径表示根目录；`ref` 可以指定分支、标签或提交，省略时使用仓库默认分支。目录响应为条目数组，文件响应包含编码和内容。[GitHub Docs](https://docs.github.com/en/rest/repos/contents?apiVersion=2026-03-10&utm_source=chatgpt.com)

| 工具 | 建议返回内容 | 第一阶段边界 |
| --- | --- | --- |
| `list_directory` | `name`、`path`、`type`、`size`、`sha` | 只列一层，不递归 |
| `read_file` | `path`、`sha`、`size`、`text` | 只支持普通 UTF-8 文本文件 |

关键处理规则：

- 根目录统一使用空字符串；其他路径使用仓库相对路径，拒绝 `..`，逐段进行 URL 编码；`ref` 作为查询参数传递。
- `list_directory` 收到文件对象时明确报错，不能把文件当目录。
- `read_file` 先检查类型、大小和 `encoding`，再清除 Base64 中的换行，严格解码为字节，最后严格转换为 UTF-8。**Base64 解码成功不代表它是文本文件。**
- 第一阶段主动设置文件上限为 **1,000,000 字节**；更大的文件、非 UTF-8 内容及 API 明确标识的特殊条目返回不支持错误，不自动访问外部目标。
- GitHub Contents API 的目录上限为 1,000 项，较大文件也存在返回格式限制；目录达到 1,000 项时提示结果可能不完整，后续再考虑 Trees API。[GitHub Docs](https://docs.github.com/en/rest/repos/contents?apiVersion=2026-03-10&utm_source=chatgpt.com)

结果使用普通字典和列表，便于以后转换成模型工具返回的 JSON。

**5. 错误处理方案**

内部抛出明确异常，由示例程序捕获并显示易读信息；避免失败时返回空列表或空字符串，因为它们也可能是正常结果。

| 情况 | 处理方式 |
| --- | --- |
| 参数或路径不合法 | 请求前报 `InvalidArgumentError` |
| 401 | 提示 Token 无效或过期 |
| 403 / 429 | 结合响应头和消息区分限流、权限不足；保留重试时间信息 |
| 404 | 提示仓库、路径或 ref 不存在，或当前身份无法访问 |
| 明确返回空仓库消息 | 报 `EmptyRepositoryError`，不把所有 404 都当空仓库 |
| 超时、连接失败、5xx | 报网络或服务错误，标记可重试 |
| 文件与目录类型不符 | 报 `WrongContentTypeError` |
| 超出大小上限、非文本内容 | 报 `UnsupportedContentError` |
| 无效 JSON、缺失字段、Base64 损坏 | 报响应格式或解码错误 |

第一版设置明确超时，**不自动重试**，让每次调用的行为容易观察。日志只记录路径、状态码及请求标识，不记录 Token。

**6. 测试计划与验收**

测试以模拟 HTTP 响应为主，真实 GitHub 调用作为手动验收。

| 测试组 | 核心用例 |
| --- | --- |
| 目录读取 | 根目录、子目录、空目录、误传文件、特殊条目、1,000 项边界 |
| 文件读取 | 英文、中文、空文件、带换行的 Base64、损坏 Base64、非 UTF-8、超限文件 |
| 参数与请求 | 中文或空格路径、非法路径、`ref`、认证头、超时配置 |
| 错误分支 | 401、权限型 403、限流型 403/429、普通 404、空仓库、5xx、网络失败、异常 JSON |

验收标准：

- 两个工具能独立调用，返回结构固定。
- 中文文件内容正确还原，空文件返回空文本。
- 错误不会伪装成成功结果。
- 自动测试无需联网或 Token。
- 手动完成一次“列目录 → 读取其中一个文本文件”。
- 你能解释 HTTP 请求、JSON 响应、Base64 字节解码和 UTF-8 文本解码之间的关系。

**7. 编码顺序**

确认后按四个小步骤推进，每步展示代码、解释并验证：

1. 建立最小项目结构和 HTTP 客户端。
2. 实现、测试 `list_directory`。
3. 实现、测试 `read_file` 和解码逻辑。
4. 补充运行示例、README，并进行真实 API 验收。

你确认这份计划后，我们先开始第 1 步。编码与提交分开处理，未经你明确要求不会提交或推送。


# 第二阶段：LLM 接入与 Agent 运行

## 任务 2.1：DeepSeek 模型适配层

### 开发范围

- 使用 DeepSeek 官方 API，默认模型为 `deepseek-flash`；模型名可通过 `DEEPSEEK_MODEL` 覆盖，并通过 OpenAI 兼容 SDK 调用。`deepseek-chat` 已于 2026-07-24 退役。
- 实现独立的模型适配接口，支持普通文本对话和 function calling。
- 将现有 `list_directory`、`read_file` 的工具定义传递给模型请求；解析并返回模型文本、工具调用名称与参数，以及可用的 token 用量信息。
- 通过环境变量管理 DeepSeek API Key；支持从项目根目录 `.env` 自动加载，已有系统环境变量优先。`.env` 不提交，不在源码、日志或示例配置中保存真实密钥。
- 处理请求超时、认证失败、限流和网络异常，并以明确的错误结果提供给上层。
- 使用 Mock 编写单元测试，并确认第一阶段现有测试继续通过。

### 验收标准

- 文本请求能够通过 Mock 验证请求参数和解析结果。
- function calling 请求能传递两个 GitHub 工具的定义，并正确解析工具名、参数及 token 用量；工具调用仅作为模型输出返回。
- 超时、认证失败、限流和网络异常均能被明确识别；错误信息和日志不包含 API Key。
- API Key 从环境变量读取；未配置时能给出清晰错误。
- 单元测试不访问真实 DeepSeek API；第一阶段全部测试仍通过。
- 本任务不直接执行 GitHub 工具，也不实现 Agent Loop。

### 交付要求

- 独立的 DeepSeek 模型适配模块及清晰、稳定的调用接口。
- 覆盖文本、function calling、token 用量及主要异常分支的 Mock 单元测试。
- 补充所需配置说明和安全的环境变量示例，不包含真实密钥。

### 开发约束

- 只负责请求模型、传递工具定义并解析模型响应；模型适配层不得导入或调用 GitHub 工具实现。
- 不在本任务中执行模型返回的工具调用，不实现多轮 Agent Loop。
- 保持模型 SDK 与 GitHub 工具层相互独立，便于单独测试和替换。

## 任务 2.2：Agent 运行层

### 实施方案

- 新增独立 `AgentRuntime`，通过稳定的 `DeepSeekAdapter.complete` 接口请求模型，并只注册现有只读工具 `list_directory`、`read_file`。
- 对外接口为 `AgentRuntime.run(task, owner=..., repo=..., ref=None)`，结果包含最终文本、结束原因、调用数、token 汇总、耗时和每个工具的状态统计；默认上限为 8 次模型交互、12 次工具调用、120 秒运行时间和每条工具结果 20,000 字符。
- 每轮维护 system/user/assistant/tool 消息；assistant tool call 以原 `tool_call_id` 配对工具结果，支持同一轮多个调用和跨轮调用。
- 对模型工具名、JSON 参数键、必填项和类型进行白名单校验；仓库 owner/repo 固定为本次运行目标，拒绝越权仓库、未知工具和任意代码/命令。
- 对工具成功结果序列化为 JSON，超出单条结果大小上限时返回明确错误，不把超长内容发给模型。结果上限最小为 256 字符，以便容纳安全错误结构。404 等可恢复错误返回脱敏工具结果供模型调整；认证/权限等不可恢复错误立即停止。
- 通过最大模型轮数、最大工具调用数、整体运行时限和单条工具结果字符上限限制资源；SDK/HTTP 请求使用剩余运行时限作为超时，重复失败的相同工具与参数不再执行。
- 记录模型调用数、工具名/状态/耗时、累计 token 用量和终止原因，不记录提示词、路径、工具参数、工具结果或凭证。
- DeepSeek V4 思考模式下，后续带工具定义的请求需要回传 assistant 的 `reasoning_content`；适配器以可选字段暴露该内容，Runtime 原样放回消息历史。

### 验收标准

- 无工具请求时返回模型最终文本；支持 list_directory → read_file 多轮过程及一轮多个工具调用。
- 未知工具、非法/多余参数和目标仓库不匹配均不执行 GitHub 请求，并生成与调用 ID 匹配的安全错误结果。
- GitHub 404 和一般业务错误会回传模型；认证/权限错误、模型 API 错误和缺少 API Key 不进入无意义重试。
- 同一失败调用被抑制；达到最大轮数、工具数或运行时限时返回明确终止原因。
- 工具结果超长时安全阻止回传；token 用量按模型响应累加；日志不包含密钥、认证头、参数和内容。
- 全部自动测试使用 Mock，不访问真实 GitHub 或 DeepSeek；第一阶段与任务 2.1 测试全部继续通过。

### 交付内容

- 独立运行层、针对 Loop/调度/错误/限制/日志的 Mock 测试，以及分析指定仓库结构的最小示例。
- 更新 README 的配置与运行说明，并保留现有第一阶段和任务 2.1 内容。

### 边界约束

- 不引入 LangChain、LangGraph、RAG、向量数据库、长期记忆、MCP 或数据库。
- 不重写 GitHub 工具或模型通信协议；仅为运行时预算及 DeepSeek 思考模式消息往返增加向后兼容的可选字段/参数。
- 只允许本次指定仓库上的只读操作；不注册写文件、提交、推送、删除、shell 或 Python 执行工具。
- Runtime 的模型适配器和 GitHub 客户端可单独注入，以支持隔离 Mock 测试。

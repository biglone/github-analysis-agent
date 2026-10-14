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

Token 从环境变量 `GITHUB_TOKEN` 读取；`.env.example` 只记录变量名，**不会自动加载 `.env`**。公开仓库可以匿名读取；需要 Token 时，由你在运行环境中配置，连接器的授权不会自动成为 Python 程序的凭证。私有仓库使用细粒度 Token 时需要对应仓库的 `Contents: read` 权限。[GitHub Docs](https://docs.github.com/en/rest/repos/contents?apiVersion=2026-03-10&utm_source=chatgpt.com)

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

# PaperFlow

> 论文辅助智能体 —— 读懂每一篇论文，并把它们变成你自己的知识。

PaperFlow 是一个本地运行的论文精读与研究助手。它把论文变成**逐段讲解**、
**结构化笔记**，并把笔记沉淀进**你的个人知识库**，让读过的每一篇论文都真正留下来。

它不是一个网页套壳，而是一套**本地 Agent 运行时**：论文解析、工具调用、
权限审批、上下文管理、知识库检索都在你自己的机器上完成。

---

## 它能做什么

### 论文讲解

把一篇论文拆开讲清楚：研究问题是什么、方法怎么做的、实验证明了什么、
结论站不站得住。支持逐段精读，也支持先给全局框架再深入细节。

### 结构化笔记

读完自动产出可直接使用的 Markdown 笔记，覆盖文献信息、研究假设、方法技术、
实验结果、贡献评价、启发与局限等维度，可直接放入 Obsidian、Notion 或
任何 Markdown 知识库。

### 个人知识库接入

通过 MCP 接入你自己的知识库（Obsidian、本地文件夹、Zotero、向量库等），
让 PaperFlow 在回答时能引用**你已有的笔记**，而不只是当前这篇论文。
读得越多，它越懂你的研究方向。

### 多篇论文对比

把多篇论文放在一起读，梳理它们之间的关系：谁延续了谁、谁反驳了谁、
同一问题的不同解法各自代价是什么。

---

## 快速开始

### 环境要求

- Python 3.12
- [uv](https://docs.astral.sh/uv/)

### 安装与启动

```bash
uv sync
```

配置模型（复制 `.env.example` 为 `.env` 后填写）：

```bash
PAPERFLOW_LLM_PROVIDER=openai
PAPERFLOW_LLM_BASE_URL=https://your-endpoint/v1
PAPERFLOW_LLM_API_KEY=sk-xxxx
PAPERFLOW_LLM_DEFAULT_MODEL=your-model
```

启动：

```bash
uv run flow-core     # 启动常驻守护进程
uv run flow-tui      # 打开交互界面（主要使用方式）
```

Windows 用户可直接双击 `start-paperflow.bat`，按菜单选择启动方式。

---

## 架构

PaperFlow 是**双进程**架构：一个常驻守护进程负责实际执行，客户端只负责交互。

```
flow-core（守护进程）
  └─ 监听 127.0.0.1:7437（TCP）
       ↑ JSON-RPC 2.0 / NDJSON
flow（命令行）    flow-tui（交互界面）
```

这种设计带来几个直接好处：

- 界面崩溃不会中断正在执行的论文分析任务
- 可以同时接入命令行、终端界面，以及后续的 Web 前端
- 所有执行过程都能通过事件流实时订阅与回放
- 命令、响应、事件全部走类型化协议，边界清晰

### 核心模块

| 模块 | 职责 |
|---|---|
| `core/bus` | 类型化 IPC 协议：命令、事件、JSON-RPC 信封 |
| `core/transport` | TCP 服务端与客户端，NDJSON 收发 |
| `core/loop` | Agent 主循环：思考 → 工具调用 → 结果回填 |
| `core/tools` | 工具注册表与内置工具（读写文件、检索、任务管理等） |
| `core/permissions` | 权限审批：写操作前先请求授权 |
| `core/session` | 会话、记忆与笔记持久化 |
| `core/compact` | 上下文水位检测与自动压缩 |
| `core/skills` | 技能（Markdown 定义的工作流，如精读、总结、综述） |
| `core/subagent` | 子智能体：并行处理多篇论文或分角色审阅 |
| `core/mcp` | 接入外部工具与个人知识库 |
| `tui` | 终端交互界面 |

### 配置优先级

内置默认值 → `~/.paperflow/config.toml` → `.env` → 环境变量。

### 主要环境变量

`PAPERFLOW_LLM_PROVIDER`、`PAPERFLOW_LLM_BASE_URL`、`PAPERFLOW_LLM_API_KEY`、
`PAPERFLOW_LLM_DEFAULT_MODEL`、`PAPERFLOW_HOST`、`PAPERFLOW_PORT`、
`PAPERFLOW_LOG_LEVEL`。

---

## 权限与隐私

论文和笔记是私人资料，PaperFlow 默认保守：

- **所有写操作需要授权**。修改、创建、删除文件前会请求许可，
  可选择允许一次、始终允许、拒绝。
- **数据留在本地**。会话、笔记、事件记录默认存放在 `~/.paperflow/`，
  不会上传到第三方。
- **只把必要内容发给模型**。你可以明确控制哪些目录、哪些文件参与分析。

---

## 开发

```bash
uv run ruff check src tests scripts   # 代码检查
uv run mypy src                       # 类型检查
uv run pytest tests/unit -v           # 单元测试
uv run pytest tests/ -v               # 全部测试（会自动拉起守护进程）
```

修改 `core/bus` 下的协议模型后，需要重新生成协议文档：

```bash
uv run python scripts/gen_protocol_doc.py
```

---

## 许可

MIT License

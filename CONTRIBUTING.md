# CONTRIBUTING.md

PaperFlow 的开发约定与架构说明。

## 常用命令

```bash
# 同步依赖
uv sync

# 代码检查
uv run ruff check src tests scripts
uv run mypy src

# 测试
uv run pytest tests/unit -v           # 仅单元测试（快，不拉起守护进程）
uv run pytest tests/integration -v    # 集成测试（夹具会自动拉起守护进程）
uv run pytest tests/ -v               # 全部

# 运行单个测试
uv run pytest tests/unit/test_envelope.py::test_request_roundtrip -v

# 修改 bus 模型后重新生成协议文档
uv run python scripts/gen_protocol_doc.py

# 校验 WIRE_PROTOCOL.md 是否与模型同步
uv run python scripts/gen_protocol_doc.py --check

# 手动启动守护进程
uv run flow-core                          # 前台运行，Ctrl+C 停止
PAPERFLOW_PORT=8000 uv run flow-core      # 指定端口

# 连通性检查
uv run flow ping
uv run flow --version
```

## 架构

PaperFlow 是**双进程**本地 Agent 系统。`flow-core` 是常驻守护进程，
`flow` 与 `flow-tui` 是连接它的客户端。

```
flow-core（守护进程）
  └─ 监听 127.0.0.1:7437（TCP）
       ↑ JSON-RPC 2.0 NDJSON
flow（命令行）   flow-tui（交互界面）
```

**`flow-tui` 是主要前端。** 所有面向用户的功能（任务管理、可观测性、交互）
都应优先在 TUI 中设计与验证。`flow` 命令行仅用于快速脚本化测试与调试，
不作为产品界面。涉及用户界面的改动，请投入在 TUI 布局、事件渲染和键盘交互上，
不要用命令行代替。

### 协议层（`src/paperflow/core/bus/`）

所有 IPC 消息都是 pydantic v2 类型化模型，通过 `type` 字段构成**可辨识联合**。
这是契约边界——新增命令或事件意味着在 `commands.py` 或 `events.py` 中新增模型类，
并扩展 `Command`/`Event` 联合类型。

- `envelope.py` — `JsonRpcRequest`、`JsonRpcSuccess`、`JsonRpcError`、错误码常量、`make_error()`
- `commands.py` — `Command` 联合类型
- `events.py` — `Event` 联合类型

`WIRE_PROTOCOL.md` 由 `scripts/gen_protocol_doc.py` 从这些模型**自动生成**。
修改 bus 模型后务必重新生成并提交。

### 传输层（`src/paperflow/core/transport/`）

- `socket_server.py` — TCP 服务端（`asyncio.start_server`）；按行读取 NDJSON，
  分发给已注册的 `CommandHandler`，处理 JSON-RPC 错误分支。`start()` 时会先探测
  `host:port`，若已有守护进程在监听则报错。处理器通过
  `server.register("method.name", handler_fn)` 注册。

### 配置（`src/paperflow/core/config.py`）

四级优先级：**内置默认值 → `~/.paperflow/config.toml` → `.env` → 环境变量**。

配置文件缺失时静默跳过；出现未知键则硬退出。

相关环境变量：`PAPERFLOW_CONFIG`、`PAPERFLOW_HOST`、`PAPERFLOW_PORT`、
`PAPERFLOW_LOG_LEVEL`、`PAPERFLOW_LOG_FILE`、`PAPERFLOW_LOG_FORMAT`。

### 守护进程入口（`src/paperflow/core/app.py`）

`CoreApp.run()` 是唯一的异步入口：加载配置 → 初始化日志 → 创建 `SocketServer`
→ 注册处理器 → 等待 `SIGINT`/`SIGTERM` → 调用 `server.stop()`。
新增处理器时，在 `CoreApp` 上实例化处理方法并调用 `server.register()`。

### 测试

`tests/conftest.py` 中的集成测试夹具会用随机空闲端口拉起真实的守护进程子进程
（`free_port` 夹具）。夹具先找到空闲端口、释放它，再通过 `PAPERFLOW_PORT`
传给守护进程，然后轮询 `asyncio.open_connection` 直到进程就绪。

## 代码风格

所有函数必须在 `def` 行上方紧跟**一行中文注释**说明其作用。例如：

```python
# 发送 JSON-RPC 响应并刷新写缓冲区
async def _send(self, writer: asyncio.StreamWriter, msg: BaseModel) -> None:
    ...
```

不要写多行 docstring，一行简洁的中文即可。

**测试函数**需要在 `def` 行上方紧跟**两行中文注释**：

```python
# 功能：验证 publish 后订阅者能收到事件对象
# 设计：用内联 handler 收集事件引用，断言 is 而非 ==，排除序列化中间步骤的干扰
async def test_publish_reaches_subscriber() -> None:
    ...
```

- `# 功能：` — 该测试验证的具体行为或不变式，一句话说清"测什么"
- `# 设计：` — 为什么选择这种测试方式：覆盖了哪些边界条件、为什么用这个
  stub/fixture、这种断言方式相比其他方式的优势

两行注释缺一不可。功能行让读者 5 秒内判断测试意图；设计行让读者理解测试背后的
决策，而非只看到操作步骤。

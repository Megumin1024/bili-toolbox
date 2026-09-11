# B站工具箱架构边界

## 总体结构

```text
main.py
  └─ app/main_window.py
       ├─ app/theme.py / app/widgets.py / app/task_page.py / app/task_runner.py
       └─ tools/__init__.py                          ← ToolSpec 注册表（7 个工具）
            ├─ tools/comments/page.py        ─┐
            ├─ tools/collector/page.py        │
            ├─ tools/data_check/page.py       ├─ 各自的 pipeline.py / core.py
            ├─ tools/report_center/page.py    │
            ├─ tools/user_dynamics/page.py    │
            ├─ tools/danmaku/page.py         ─┘
            └─ tools/monitor/page.py
                         │
                         ├─ tools/monitor/server.py → core/
                         └─ tools/monitor/static/index.html

core/                                    ← 不允许 import app/ 或 tools/
  ├─ 网络通道：session.py / client.py / transport.py
  │            proxy.py / fingerprint.py / activation.py / wbi.py
  ├─ 可靠性内核：net_errors.py / backoff.py / cancel.py / gate.py
  │              risk.py / redact.py
  ├─ 配置与状态：config.py / task_history.py / task_presets.py
  └─ 输出与文本：output.py / xlsx.py / text.py / links.py / diagnostics.py
```

## 依赖方向

允许的方向如下：

| 层 | 可以依赖 | 不应依赖 |
| --- | --- | --- |
| `main.py` | `app`、启动所需的 `core` | 具体采集实现细节 |
| `app/` | Qt、公共组件、配置、任务运行器 | 其他工具的内部实现 |
| `tools/<feature>/page.py` | `app`、本工具的 `pipeline.py`、必要的 `core` | 其他工具目录 |
| `tools/<feature>/pipeline.py` | 本工具的 `core.py`、共享 `core` | 其他工具的 `page.py` 或 `pipeline.py` |
| `tools/monitor/server.py` | 共享 `core` | Qt 页面实现 |
| `tools/monitor/static/` | 现有 HTTP JSON 契约 | Python 内部模块 |
| `core/` | Python 标准库和第三方基础依赖 | `app`、`tools` |

`tools/__init__.py` 是注册表例外：它可以导入各工具的页面，并通过 `ToolSpec` 注册到侧边栏。

## 共享契约

以下内容是跨功能的稳定契约，修改时必须有明确任务和回归验证：

- `TaskRunner` 的开始、进度、日志、取消和完成信号。
- 配置文件已有字段及旧配置的默认值。
- JSONL/XLSX 字段、文件命名和断点续传行为。
- 监控 HTTP API 的响应字段、状态和历史数据结构。
- `main.py` 中的冻结 DLL 搜索路径处理。
- `build_toolbox.spec` 的静态资源和 protobuf 隐式导入。
- `core/net_errors.py` 的 `ErrorKind` 分类口径——所有重试/退避/熔断判断都挂在它上面，改它会牵动整个网络层。
- `core/gate.py` 的 `shared_gate()` 进程级单例语义——session 单例与监控自建 client 共用同一份背压。
- 二进制通道（`get_bytes` / `fetch_bytes`）与 JSON 通道共用同一套闸门、重试、统计和探针回报。

## 修改范围判断

| 需求 | 默认允许修改 | 必须额外检查 |
| --- | --- | --- |
| 普通页面 | 对应 `page.py`、`app/widgets.py` 或主题文件 | 页面导航、主题、缩放和截图 |
| 新任务功能 | 新工具目录、注册表、相关文档和测试 | 参数校验、进度、取消、输出 |
| 监控指标 | `server.py`、静态页面和契约测试 | 服务端与前端字段同时兼容 |
| 配置字段 | 配置读写和设置页 | 旧配置启动、保存、重启恢复 |
| 依赖/打包 | `requirements.txt`、`main.py`、spec、环境脚本 | 官方 Python、干净 PATH、EXE 启动 |
| 共享 `core/` | 只有明确授权时 | 全部 7 个工具的回归测试和保护性 diff；`scripts/check_boundaries.py` |

## 防止功能杂糅的规则

1. 一个任务只对应一个结果，不把顺手重构其他模块混进来。
2. 一个功能只拥有自己的页面、流水线和数据处理代码。
3. 跨工具共享代码必须先定义公共接口，再迁移实现。
4. 结构约束必须由 `scripts/check_boundaries.py` 等检查工具执行，不能只依赖人工记忆。
5. 任务完成必须留下验证命令和结果；失败要保留原始错误和影响范围。

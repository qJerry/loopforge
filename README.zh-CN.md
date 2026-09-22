<div align="center">

# LoopForge

**面向长时运行编码 Agent 的本地控制面。**

注册一次仓库，之后由 LoopForge 决定下一步该让 Agent 推进哪个项目、
在独立的 git worktree 里执行、完整记录每一轮运行，最后把 diff 交给你。

[![License: MIT](https://img.shields.io/badge/license-MIT-black.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-black.svg)](https://www.python.org/)
[![Dependencies: none](https://img.shields.io/badge/runtime%20deps-0-black.svg)](pyproject.toml)
[![Agents: Claude Code · Codex](https://img.shields.io/badge/agents-Claude%20Code%20%C2%B7%20Codex-black.svg)](#worker-适配器)

[English](README.md) · [简体中文](README.zh-CN.md)

</div>

---

## 要解决的问题

编码 Agent 在十分钟的尺度上很出色，在十小时的尺度上并不可靠。

终端会话不适合承载以“天”为单位的工作：合上电脑它就没了；它不记得前三次尝试做过什么；
它会毫无察觉地让两个 Agent 同时改同一份工作区；它不留审计链、不算成本，
也回答不了「凌晨两点到六点它到底做了什么、为什么停下来」。

而这还只是一个仓库。多数人手上有六个。

## LoopForge 是什么

LoopForge 是一层小而无趣的本地基础设施，位于**你和你已经装好的 Agent CLI 之间**。
它不封装模型 API，不发明 prompt 格式，也不想变成你的 IDE。

它只负责会话本身无法负责的四件事：

| | |
|---|---|
| **调度** | 一次 tick 只把一个项目推进一步。项目级频率、串行执行，不会一拥而上。 |
| **隔离** | 每个任务拥有受管分支和独立 worktree，Agent 不会碰你正在编辑的工作区。 |
| **持久** | Worker 以独立进程运行。关掉控制台、重启界面，运行照常继续。状态是提交进 git 的纯 JSON。 |
| **可追责** | 每轮运行都追加到只增账本：跑了什么、改了什么、被什么卡住、花了多少 token。 |

其余的事情——规划、代码品味、判断力——仍然留在 Agent 和你自己手里。

---

## 架构

![LoopForge 架构](docs/architecture.svg)

控制面是 **约 7k 行零依赖 Python**，控制台是由同一个进程托管的单个 React 产物。
没有数据库、没有消息队列、没有需要安装的守护进程：状态就是你现有仓库里的 JSON 文件。

## 一次 tick 的全过程

![一次 LoopForge tick](docs/run-loop.svg)

Worker 就是一个普通子进程：从 stdin 接收 prompt，必须返回结构化的 `worker-result.json`——
状态、摘要、产物、token 成本。LoopForge 从不靠解析自然语言来判断任务是否成功。

---

## 功能

- **多项目注册** —— 任意 git 仓库都可以带 profile 注册：worker、模型、调度频率、自动化档位。
- **串行调度** —— 每个项目可选每半小时 / 每小时 / 每天 / 每周，一次 tick 最多选一个项目。
- **项目级锁** —— 同一仓库同时只有一轮运行；worker 被强杀时能识别陈旧锁。
- **独立分发** —— 运行不随控制台重启中断；`active-dispatch.json` 与 PID 跟踪让孤儿进程可见而非静默。
- **任务账本** —— 每个任务一个 JSON 文件，`expected_version` 乐观并发、原子写入，终态任务归档到 `history/YYYY/MM/`。
- **Git 原生发布** —— 受管 `feature/<task-id>` 分支、每任务一个 worktree，账本提交回默认分支，于是 git 历史本身就是审计链。
- **可插拔 Worker** —— Claude Code CLI、Codex CLI，或用于演示与 CI 的零 token 假 worker。
- **项目级网络** —— 继承环境、直连，或只向 worker 子进程注入指定代理，绝不改动你的 shell。
- **健康检测** —— 在浪费一轮运行之前就发现 CLI 缺失、登录过期或端点被拦，且不消耗模型额度。
- **Token 计量** —— 基于 worker 自身用量上报，做 per-run 与 per-task 成本归集。
- **Webhook 通知** —— 阻塞 / 超时 / 完成时触发，并做去重，一个卡住的任务不会连轰你四十条。
- **接入脚手架** —— 一条命令为新仓库生成面向 Agent 的文档与测试入口（`go-backend`、`react-frontend`、`flutter-app`、`java-backend`、`python-scripts`、`project-group`）。
- **默认只跑本地** —— 绑定 `127.0.0.1`、bearer token 校验、无遥测；除了 Agent CLI 和你自己配置的 webhook，不发起任何外部请求。

---

## 快速开始

**依赖：** Python 3.9+、git，以及 `PATH` 上至少一个 Agent CLI
（[Claude Code](https://claude.com/claude-code) 或 Codex）。只有需要重新构建控制台时才需要 Node 20+。

```bash
git clone https://github.com/OWNER/loopforge.git
cd loopforge
python3 -m unittest discover -s tests   # 可选：全部标准库，约 60 秒
npm install && npm run build:console    # 可选：web/console 已带预构建产物
npm run start:local
```

然后打开：

```
http://127.0.0.1:8765/?token=loopforge-local
```

仓库内置一个由零 token `fake_codex` 驱动的 **demo 项目**，可以在把 LoopForge 指向真实仓库之前，
先完整看一遍任务如何走完状态机。点击「调度一轮」即可。

### 默认值

| 配置 | 环境变量 | 默认值 |
|---|---|---|
| 后端端口 | `LOOPFORGE_PORT` | `8765` |
| 前端开发端口 | `LOOPFORGE_FRONTEND_PORT` | `5173` |
| API token | `LOOPFORGE_TOKEN` | `loopforge-local` |
| 项目注册表 | `LOOPFORGE_CONFIG` | `.loopforge/projects.json` |
| 后台自动调度 | `LOOPFORGE_AUTO_SCHEDULE` | `1` |
| tick 间隔（秒） | `LOOPFORGE_SCHEDULE_INTERVAL` | `60` |
| Claude 可执行文件 | `LOOPFORGE_CLAUDE_BIN` | 自动探测 |
| Codex 可执行文件 | `LOOPFORGE_CODEX_BIN` | 自动探测 |

---

## 接入你自己的仓库

接入是两步、带 hash 确认的操作：先预检出一份计划，再原样应用这份计划。

```bash
# 1 · 预检 —— 不写任何文件
python3 -m loopforge.cli --config .loopforge/projects.json \
  project onboard --root /path/to/your/repo --type go-backend --owner you --dry-run --json

# 2 · 应用 —— 必须携带第一步返回的 hash
python3 -m loopforge.cli --config .loopforge/projects.json \
  project onboard --root /path/to/your/repo --type go-backend --owner you \
  --apply --plan-hash <第一步的 hash> --json

# 3 · 校验接线
python3 -m loopforge.cli --config .loopforge/projects.json project doctor <project-id> --json
```

控制台左侧提供同样的流程。接入只会向仓库补充面向 Agent 的文档和项目契约，不会改写你的源码。

### 驱动它

```bash
# 推进当前到期的项目
python3 -m loopforge.cli --config .loopforge/projects.json schedule-tick --json

# 只看「将会执行什么」，不写任何任务文件
python3 -m loopforge.cli --config .loopforge/projects.json project preview-run <project-id> --json

# 手动推进某个项目一轮
python3 -m loopforge.cli --config .loopforge/projects.json project run-once <project-id> --json

# 全局状态
python3 -m loopforge.cli --config .loopforge/projects.json status --json
```

所有命令默认输出人类可读文本，加 `--json` 输出机器可读结构，
因此 cron、CI 或你自己的编排器都可以驱动 LoopForge，而不必去解析自然语言。

### HTTP API

所有路由都需要 `Authorization: Bearer $LOOPFORGE_TOKEN`，且只监听回环地址。

| 方法 | 路由 | 用途 |
|---|---|---|
| `GET` | `/api/v1/projects` | 全部项目的总览快照 |
| `GET` | `/api/v1/projects/{id}` | 项目详情、当前任务、最近一轮运行 |
| `POST` | `/api/v1/projects/{id}/tasks` | 创建任务 |
| `GET` | `/api/v1/projects/{id}/tasks` | 活跃任务列表 |
| `GET` | `/api/v1/projects/{id}/runs` | 运行历史 |
| `GET` | `/api/v1/projects/{id}/events` | 事件历史 |
| `POST` | `/api/v1/schedule-tick` | 手动推进一次调度 |
| `POST` | `/api/v1/projects/{id}/pause` | 让项目退出调度 |
| `POST` | `/api/v1/projects/{id}/cancel-run` | 请求正在运行的 worker 停止 |
| `GET` | `/api/v1/worker-health` | Agent CLI 可达性与登录状态 |
| `GET` | `/api/v1/token-usage/tasks` | 成本归集 |

受理型分发路由返回 `202`，携带 `dispatch_id` 与 worker PID，不会阻塞等待 Agent。

### Worker 适配器

| Executor | 命令 | 说明 |
|---|---|---|
| `claude_cli` | `claude -p --verbose …` | 模型 `opus` / `sonnet` / `haiku`；权限模式 `acceptEdits` 或 `bypassPermissions` |
| `codex_cli` | `codex exec --sandbox … --json …` | 可配置推理强度与沙箱级别 |
| `fake_codex` | *(无)* | 确定性、离线、零 token —— demo 项目与测试套件使用 |

新增第三个 executor 只需实现两个函数：拼 argv、解析结果。没有额外的插件框架要学。

---

## 磁盘布局

没有任何东西藏在数据库里。每个项目：

```
<your-repo>/
├── data/tasks/<task-id>.json        # 活跃任务账本，提交进 git
├── data/tasks/history/YYYY/MM/…     # 归档的终态任务，不可修改
└── .loopforge/
    ├── index.jsonl                  # 只增运行历史
    ├── events.jsonl                 # 只增审计事件
    ├── lock.json                    # 项目锁
    ├── active-dispatch.json         # 当前 worker 占用
    └── dispatches/<id>/             # request.json · runner.log · result.json
```

## 安全模型

LoopForge 会让 Agent 拿到真实的文件系统权限。与其粉饰，不如说清楚：

- HTTP 服务绑定 `127.0.0.1`，没有 bearer token 一律拒绝。
- Worker 在**专属 worktree** 中执行，而不是你正在使用的工作区。
- 代理 URL 不允许内嵌账号密码——请指向一个持有凭据的本地代理。
- `bypassPermissions` 可用于无人值守运行，危险程度与字面意思一致。只在你随时可以 `git reset` 的仓库上开。
- 无遥测、无回传、无内置埋点。

---

## 路线图

以下是**尚未实现**的部分，如实列出。欢迎贡献与讨论。

- [ ] **并行执行** —— 目前调度器是有意串行的；下一个结构性改动是项目级并发加全局预算。
- [ ] **直连 API 的 Worker** —— 面向没有 CLI 的环境，直接对接模型 API 的适配器。
- [ ] **更多通知渠道** —— Slack、Discord 与通用 JSON webhook（当前只有 webhook 渠道）。
- [ ] **成本预算** —— 已有 token 计量，但还没有任何上限约束。
- [ ] **远程 / 无头模式** —— 需要一套能走出回环地址的鉴权模型。
- [ ] **运行重放** —— 用不同模型重跑已记录的 prompt，对比行为差异。
- [ ] **打包安装** —— `pipx install loopforge` 并内置控制台。
- [ ] **英文控制台** —— UI 文案目前是中文，i18n 抽取尚未完成。
- [ ] **Windows 支持** —— 目前只在 macOS 上开发和测试，git worktree 与进程处理需要重新审计。

---

## 设计取舍

**为什么串行？** 因为并行编码 Agent 的典型故障不是慢，而是两个 Agent 同时 rebase 同一个分支。
并发应该在隔离被验证之后再加，而不是之前。

**为什么把 JSON 放在仓库里？** 因为审计链应该比工具活得久。哪怕明天删掉 LoopForge，
`git log` 依然能告诉你 Agent 在什么时候做了什么。

**为什么调用 CLI 而不是直接调 API？** 因为 Agent CLI 已经把工具调用、权限和上下文管理解决得不错。
重新实现一遍，只会得到一个更差的 Agent 和更大的维护成本。

**为什么代码注释是中文？** 它最初是作者的个人工具。协议值、API 和 CLI 都是英文，
只有散文部分还没翻译。欢迎提 PR。

---

## 项目状态

**Alpha。** 作者每天在多个仓库上真实使用；API 与磁盘格式仍允许变更。
测试套件就是契约——任何行为存疑时，`tests/` 是本仓库最准确的文档。

## 贡献

欢迎 issue 和 PR，尤其是路线图中的条目。提 PR 前请先运行
`python3 -m unittest discover -s tests`——整套测试离线执行，不需要任何 API key。

## 许可

[MIT](LICENSE)

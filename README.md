<div align="center">

# LoopForge

**An AI-driven engineering delivery platform for long-running coding agents.**

Register your repositories once. LoopForge decides *which* project an agent works on next,
runs it in an isolated git worktree, records every run, and hands you the diff.

[![License: MIT](https://img.shields.io/badge/license-MIT-black.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-black.svg)](https://www.python.org/)
[![Dependencies: none](https://img.shields.io/badge/runtime%20deps-0-black.svg)](pyproject.toml)
[![Agents: Claude Code · Codex](https://img.shields.io/badge/agents-Claude%20Code%20%C2%B7%20Codex-black.svg)](#worker-adapters)

[English](README.md) · [简体中文](README.zh-CN.md)

</div>

---

## The problem

Coding agents are excellent for ten minutes and unreliable for ten hours.

A terminal session is the wrong container for work that takes a day. It dies when you close the
laptop. It has no memory of the last three attempts. It happily runs two agents in the same working
tree. It leaves no audit trail, no cost accounting, and no way to answer *"what did it actually do
between 2am and 6am, and why did it stop?"*

And that is one repository. Most people have six.

## What LoopForge is

LoopForge is a small, boring, local piece of infrastructure that sits **between you and the agent
CLI you already have installed**. It does not wrap a model API, it does not invent a prompt format,
and it does not want to be your IDE.

It owns the four things a session cannot own:

| | |
|---|---|
| **Scheduling** | One tick advances exactly one project by exactly one step. Per-project cadence, serial by design, no thundering herd of agents. |
| **Isolation** | Every task gets a managed branch and its own git worktree. The agent never touches the checkout you are typing in. |
| **Durability** | Workers run detached. Close the console, reboot the UI, the run survives. State lives in plain JSON, committed to git. |
| **Accountability** | Every run appends to an append-only ledger: what ran, what changed, what blocked it, how many tokens it cost. |

Everything else — planning, code quality, taste — stays where it belongs: in the agent, and in you.

---

## Architecture

![LoopForge architecture](docs/architecture.svg)

The platform's core is **~7k lines of dependency-free Python**. The console is a single React bundle
served by that same process. There is no database, no broker, no daemon to install: state is JSON
files inside the repositories you already have.

## Anatomy of one tick

![One LoopForge tick](docs/run-loop.svg)

The worker is a plain subprocess. It receives a prompt on stdin and is required to answer with a
structured `worker-result.json` — status, summary, artifacts, token cost. LoopForge never parses
prose to decide whether a task succeeded.

---

## Features

- **Multi-project registry** — register any git repository with a profile: worker, model, cadence, automation level.
- **Serial scheduler** — half-hourly / hourly / daily / weekly per project; the tick picks at most one.
- **Project locks** — a repository is driven by one run at a time, with stale-lock detection when a worker is killed.
- **Detached dispatch** — runs survive console restarts; `active-dispatch.json` + PID tracking makes orphans visible instead of silent.
- **Task ledger** — one JSON file per task, optimistic `expected_version` concurrency, atomic writes, terminal tasks archived to `history/YYYY/MM/`.
- **Git-native publishing** — managed `feature/<task-id>` branches, one worktree per task, the ledger committed to your default branch so the history *is* the audit log.
- **Pluggable workers** — Claude Code CLI, Codex CLI, or a zero-token fake worker for demos and CI.
- **Per-project network** — inherit the environment, go direct, or inject an explicit proxy into the worker subprocess only. Never mutates your shell.
- **Health checks** — detects a missing CLI, a stale login, or a blocked endpoint *before* burning a run. No model call required.
- **Token accounting** — per-run and per-task cost rollups from the worker's own usage report.
- **Webhook notifications** — fire on blocked / timeout / completion, with de-duplication so one stuck task does not page you forty times.
- **Onboarding scaffolds** — one command drops agent-facing docs and a test entry point into a new repo (`go-backend`, `react-frontend`, `flutter-app`, `java-backend`, `python-scripts`, `project-group`).
- **Local-only by default** — binds `127.0.0.1`, bearer-token guarded, no telemetry, no outbound calls except the agent CLI and any webhook you configure.

---

## Quickstart

**Requirements:** Python 3.9+, git, and at least one agent CLI on your `PATH`
([Claude Code](https://claude.com/claude-code) or Codex). Node 20+ only if you want to rebuild the console.

```bash
git clone https://github.com/OWNER/loopforge.git
cd loopforge
python3 -m unittest discover -s tests   # optional: everything is stdlib, this takes ~60s
npm install && npm run build:console    # optional: a prebuilt console ships in web/console
npm run start:local
```

Then open:

```
http://127.0.0.1:8765/?token=loopforge-local
```

The repository ships with a **demo project** backed by the zero-token `fake_codex`, so you can watch
a task move through the full state machine before pointing LoopForge at anything you care about.
Press **Schedule a tick** and follow the run.

### Defaults

| Setting | Env var | Default |
|---|---|---|
| Backend port | `LOOPFORGE_PORT` | `8765` |
| Console dev port | `LOOPFORGE_FRONTEND_PORT` | `5173` |
| API token | `LOOPFORGE_TOKEN` | `loopforge-local` |
| Project registry | `LOOPFORGE_CONFIG` | `.loopforge/projects.json` |
| Background scheduling | `LOOPFORGE_AUTO_SCHEDULE` | `1` |
| Tick interval (seconds) | `LOOPFORGE_SCHEDULE_INTERVAL` | `60` |
| Claude binary | `LOOPFORGE_CLAUDE_BIN` | auto-discovered |
| Codex binary | `LOOPFORGE_CODEX_BIN` | auto-discovered |

---

## Integrating your own repository

Onboarding is a two-step, hash-confirmed operation: you preview a plan, then apply exactly that plan.

```bash
# 1 · preview — writes nothing
python3 -m loopforge.cli --config .loopforge/projects.json \
  project onboard --root /path/to/your/repo --type go-backend --owner you --dry-run --json

# 2 · apply — requires the hash returned by the preview
python3 -m loopforge.cli --config .loopforge/projects.json \
  project onboard --root /path/to/your/repo --type go-backend --owner you \
  --apply --plan-hash <hash-from-step-1> --json

# 3 · verify the wiring
python3 -m loopforge.cli --config .loopforge/projects.json project doctor <project-id> --json
```

The same flow is available in the console sidebar. Onboarding adds agent-facing docs and a project
contract to your repository; it never rewrites your source.

### Driving it

```bash
# advance whichever project is due
python3 -m loopforge.cli --config .loopforge/projects.json schedule-tick --json

# see what *would* run, without touching any task file
python3 -m loopforge.cli --config .loopforge/projects.json project preview-run <project-id> --json

# advance one specific project once
python3 -m loopforge.cli --config .loopforge/projects.json project run-once <project-id> --json

# status of everything
python3 -m loopforge.cli --config .loopforge/projects.json status --json
```

Every command is human-readable by default and machine-readable with `--json`, so cron, CI, or your
own orchestrator can drive LoopForge without scraping output.

### HTTP API

All routes require `Authorization: Bearer $LOOPFORGE_TOKEN` and bind to loopback only.

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/v1/projects` | dashboard snapshot for every project |
| `GET` | `/api/v1/projects/{id}` | project detail, current task, latest run |
| `POST` | `/api/v1/projects/{id}/tasks` | create a task |
| `GET` | `/api/v1/projects/{id}/tasks` | active task list |
| `GET` | `/api/v1/projects/{id}/runs` | run history |
| `GET` | `/api/v1/projects/{id}/events` | event history |
| `POST` | `/api/v1/schedule-tick` | advance the scheduler once |
| `POST` | `/api/v1/projects/{id}/pause` | drop a project out of scheduling |
| `POST` | `/api/v1/projects/{id}/cancel-run` | ask the active worker to stop |
| `GET` | `/api/v1/worker-health` | agent CLI reachability and login state |
| `GET` | `/api/v1/token-usage/tasks` | cost rollup |

Accepted dispatch routes answer `202` with a `dispatch_id` and the worker PID — they never block on
the agent.

### Worker adapters

| Executor | Command | Notes |
|---|---|---|
| `claude_cli` | `claude -p --verbose …` | model `opus` / `sonnet` / `haiku`; permission mode `acceptEdits` or `bypassPermissions` |
| `codex_cli` | `codex exec --sandbox … --json …` | configurable reasoning effort and sandbox level |
| `fake_codex` | *(none)* | deterministic, offline, zero tokens — used by the demo project and the test suite |

Adding a third executor means implementing two functions: build the argv, parse the result. There is
no plugin framework to learn.

---

## On-disk layout

Nothing is hidden in a database. Per project:

```
<your-repo>/
├── data/tasks/<task-id>.json        # active task ledger, committed to git
├── data/tasks/history/YYYY/MM/…     # archived terminal tasks, immutable
└── .loopforge/
    ├── index.jsonl                  # append-only run history
    ├── events.jsonl                 # append-only audit events
    ├── lock.json                    # project lock
    ├── active-dispatch.json         # current worker claim
    └── dispatches/<id>/             # request.json · runner.log · result.json
```

## Security model

LoopForge runs agents with real filesystem access. It is honest about that rather than pretending
otherwise:

- The HTTP server binds `127.0.0.1` and rejects any request without the bearer token.
- Workers execute inside a **dedicated worktree**, not your working checkout.
- Proxy URLs may not embed credentials — point at a local proxy that holds them instead.
- `bypassPermissions` is available for unattended runs and is exactly as dangerous as it sounds. Use it on repositories you can `git reset`.
- No telemetry, no phone-home, no bundled analytics.

---

## Roadmap

Honest list of what is **not** built yet. Contributions and opinions welcome.

- [ ] **Parallel execution** — the scheduler is serial by design today; per-project concurrency with a global budget is the next structural change.
- [ ] **Direct API workers** — an adapter that talks to a model API instead of shelling out to a CLI, for environments without one.
- [ ] **More notification channels** — Slack, Discord, and a generic JSON webhook (only a webhook channel exists now).
- [ ] **Cost budgets** — token accounting exists, but nothing enforces a ceiling yet.
- [ ] **Remote / headless mode** — an auth model that survives leaving loopback.
- [ ] **Run replay** — re-run a recorded prompt against a different model to compare behaviour.
- [ ] **Packaged install** — `pipx install loopforge` with the console bundled.
- [ ] **English console** — the UI strings are currently Chinese; i18n extraction is not done.
- [ ] **Windows support** — developed and tested on macOS; the git-worktree and process handling need auditing elsewhere.

---

## Design notes

**Why serial?** Because the failure mode of parallel coding agents is not slowness, it is two agents
rebasing the same branch. Concurrency is a feature to add once isolation is proven, not before.

**Why JSON files in the repo?** Because the audit trail should outlive the tool. If you delete
LoopForge tomorrow, `git log` still tells you what the agent did and when.

**Why shell out to a CLI instead of calling an API?** Because the agent CLIs already solve tool use,
permissions, and context management well. Re-implementing that is how you get a worse agent and a
bigger maintenance bill.

**Why is the code commented in Chinese?** It was built as a personal tool first. The protocol values,
API, and CLI are English; the prose has not been translated yet. Pull requests welcome.

---

## Status

**Alpha.** Used daily by its author across several repositories; the API and on-disk format are
still allowed to change. The test suite is the contract — if you are unsure how something behaves,
`tests/` is the most accurate documentation in the repository.

## Contributing

Issues and pull requests are welcome, especially for the roadmap items above. Please run
`python3 -m unittest discover -s tests` before opening a PR — the suite is offline and needs no
API keys.

## License

[MIT](LICENSE)

# RimSort - 项目指南

> 本文原名 `AGENTS.md`，2026-09-30 改名：全工作区只保留工作区根目录一份 `AGENTS.md`
> （统一入口，含本项目要点、环境事实与跨项目铁律）。本文件保留本仓库的深度细节。
> 注：本文件与 `opencode.json` 都是本地分支 `dev-local-fedora` 新增的，不属于上游。
>
> **远端与同步（2026-09-30）**：`origin` = 自己的 fork `uitstalie/RimSort`，`upstream` = `RimSort/RimSort`。
> 可以把 `dev-local-fedora` push 到 `origin`，**不要 push 到 `upstream`**。
> 同日已 merge 上游 107 个提交；唯一冲突是 `rimsort.nuitka-package.config.yml`（本地删除、上游修改），
> 处理为**保持删除**（本分支不用 Nuitka），故每次同步上游都会再冲突一次 —— 属已知代价。
> 上游自带的 `RimSort/AGENTS.md` 是上游开发规范，与本工作区根目录的 `AGENTS.md` 并存。

RimWorld mod manager. Python 3.12 + PySide6, MVC architecture.

## Quick Start

```shell
uv sync
git submodule update --init --recursive
uv run python -m app
```

## Common Commands

```shell
just run
just test
just lint
just format
just typecheck
just ci
just build
uv run python distribute.py --help
```

## Architecture

- `app/controllers/` — MVC controllers
- `app/views/` — MVC views
- `app/models/` — data models
- `app/utils/` — utilities, including `steam/`, `todds/`, `rentry/`
- `app/sort/` — sorting algorithms
- `app/cli/` — CLI interface
- `app/windows/` — additional window panels

## Key Singletons

- `AppInfo` (`app/utils/app_info.py`) — application paths, version
- `MetadataManager` (`app/utils/metadata.py`) — mod metadata management
- `EventBus` (`app/utils/event_bus.py`) — Qt signal bus

## Code Style

- Linter/Formatter: ruff (`pyproject.toml`)
- Type checking: mypy strict, PyRight
- Tests: pytest + pytest-qt

## Working Notes

- Prefer the existing Python 3.12 + PySide6 MVC structure.
- Keep documentation and code changes logically separated.
- Do not depend on `.claude/` files; OpenCode project instructions live here and in `opencode.json`.

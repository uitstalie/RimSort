# RimSort

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

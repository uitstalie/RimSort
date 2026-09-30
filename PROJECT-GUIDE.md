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

## 本机特有改动：Steam 启动时注入中文字体

**问题**：勾选 "Launch via Steam protocol" 启动时，游戏里**中文全部空白**（拉丁字母正常）；
直接 exec 启动则正常。

**根因**（2026-10-01 实测）：

1. Unity 的 Linux 播放器**只扫描 `/usr/share/fonts` 一个目录**，且**完全不使用 fontconfig**
2. Steam 给原生 Linux 游戏套的 scout/soldier 容器里，该目录只有 6 个 DejaVu **拉丁**字体；
   宿主的字体被挂在 `/run/host/fonts`，Unity 不读 ⇒ 容器内无中文字体可用
3. 容器根是**对运行时平台目录做硬链接**，参与链接的文件由
   `<platform>/usr-mtree.txt.gz` **清单**决定 ⇒ 只放文件、不改清单会被忽略

**实现**：`app/utils/steam/linux_runtime_fonts.py`

- `ensure_cjk_fonts_in_steam_runtimes()` 在 **Steam 协议启动分支**里调用
  （`app/views/main_content_panel.py` 的 `_do_run_game`，调用点用 `sys.platform == "linux"` 守卫）
- **Steam 目录发现**（Linux 上位置并不唯一）：先取**游戏所在库**（`<库>/steamapps/common`，
  非默认库只靠这个才找得到），再补 `~/.local/share/Steam`、`~/.steam/steam`、
  **Flatpak** `~/.var/app/com.valvesoftware.Steam/...`，并解析各根的
  `libraryfolders.vdf` 取**所有**库；去重（解析符号链接）、跳过不存在项
- 把宿主的中文 **`.ttf`**（`DroidSansFallbackFull.ttf`、`simhei.ttf`）复制进该平台
  `files/share/fonts/cjk/`，并把条目写进平台清单（首次备份为 `.orig`）
- **只修每个运行时家族"最新"的平台目录**：平台目录名内嵌可排序版本号，Steam 只运行最新那个；
  全量复制 21 个目录约 265 MB，只修最新约 55 MB
- 幂等、失败只告警不阻断启动（与 `process_nice` 的处理风格一致）
- 测试：`tests/utils/steam/test_linux_runtime_fonts.py`（14 项，含目录发现与幂等）

**注意**：

- **必须 `.ttf`/`.otf`**：Unity 不认 `.ttc` 字体集合
- **每次 Steam Linux Runtime 更新后**需再触发一次（更新会换掉 `*_platform_*` 目录）。
  升级后随便启动一次游戏即可自动修复；也可离线运行
  `common/rimworld-linux/restore-container-cjk-fonts.py`（工作区里的等价独立脚本）
- 已实测**无效**、勿再试：`PRESSURE_VESSEL_FILESYSTEMS_RO` 挂字体目录、
  往 `~/.fonts`/`~/.local/share/fonts` 放字体、去掉 `start_RimWorld.sh` 的 `LC_ALL=C`

## Working Notes

- Prefer the existing Python 3.12 + PySide6 MVC structure.
- Keep documentation and code changes logically separated.
- Do not depend on `.claude/` files; OpenCode project instructions live here and in `opencode.json`.

# TODO

- RimSort：`origin` 已换为自己的 fork（`uitstalie/RimSort`），`upstream` 才是 `RimSort/RimSort`。
  只 push `origin`，不要 push `upstream` #constraint
- 2026-09-30 已 merge 上游 107 个提交到 `dev-local-fedora` 并推送 fork；
  冲突文件 `rimsort.nuitka-package.config.yml` 保持删除（不用 Nuitka），每次同步上游会再冲突 #note
- 上游现在自带 `AGENTS.md`（合并带入），RimSort 仓库内的开发规范以它为准；
  工作区统一入口是工作区根目录的 `AGENTS.md`，本仓库另有 `PROJECT-GUIDE.md` #note

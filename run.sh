#!/bin/bash
# RimSort 启动脚本 for Fedora 43

cd "$(dirname "$0")"
uv run python -m app "$@"

#!/usr/bin/env bash
# Reinstalls the global `daimon` CLI (`uv tool install --editable`) and
# regenerates the local dev venv, pointed at wherever this checkout
# currently lives. Idempotent — safe to rerun any time, and specifically
# needed after moving/renaming the daimon project folder: both
# `uv tool install --editable` and `uv sync` bake the absolute source path
# in at install time, so a move otherwise leaves the global `daimon` command
# and agents/.venv pointing at a path that no longer exists.
set -euo pipefail

AGENTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "reinstalling daimon CLI (editable) from $AGENTS_DIR"
uv tool install --editable "$AGENTS_DIR" --force

echo "regenerating agents/.venv"
(cd "$AGENTS_DIR" && uv sync)

echo "done — 'daimon' on PATH now points at $AGENTS_DIR"

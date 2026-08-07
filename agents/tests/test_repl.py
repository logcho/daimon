"""The kernel REPL — a real Jupyter kernel keeps state across calls."""

from __future__ import annotations

import pytest

from daimon_agent.tools.repl import ReplManager


@pytest.mark.asyncio
async def test_kernel_persists_state_across_calls(tmp_path) -> None:
    repl = ReplManager(tmp_path)
    try:
        await repl.start()
        first = await repl.execute("x = 1")
        second = await repl.execute("x + 1")
        assert "2" in second
        assert "1" not in first  # assignment produces no output
    finally:
        await repl.stop()


@pytest.mark.asyncio
async def test_kernel_reports_errors_as_text(tmp_path) -> None:
    repl = ReplManager(tmp_path)
    try:
        await repl.start()
        result = await repl.execute("1 / 0")
        assert "Error" in result
        assert "ZeroDivisionError" in result
        # The kernel survives the error and keeps its state.
        assert "4" in await repl.execute("2 + 2")
    finally:
        await repl.stop()


@pytest.mark.asyncio
async def test_kernel_prints_stdout(tmp_path) -> None:
    repl = ReplManager(tmp_path)
    try:
        await repl.start()
        result = await repl.execute("print('hello from the kernel')")
        assert "hello from the kernel" in result
    finally:
        await repl.stop()

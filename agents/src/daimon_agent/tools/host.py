"""Host-facing tools — pure event emissions; the daemon/UI performs the real
action. "Stage, don't execute" is the invariant for the terminal: the agent
never presses Enter on the user's behalf."""

from __future__ import annotations

from ..emitter import emit
from ..events import ui_action_event


def stage_terminal_command(command: str) -> str:
    """Stage a shell command in a new terminal tab WITHOUT running it — the
    user presses Enter themselves. Port of legacy open_terminal_with_command."""
    emit(ui_action_event(command))
    return (
        f'Opened a new terminal tab in Daimon and typed "{command}" into it — staged only, '
        f"not executed. The user needs to press Enter themselves to actually run it."
    )

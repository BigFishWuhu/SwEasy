# Repository Agent Instructions

## Command-line tools

GNU Coreutils for Windows is installed and available in PATH. Prefer GNU
Coreutils commands for common filesystem and text-processing operations when
practical. Use PowerShell or native Windows commands for Windows-specific work.

## Game UI automation

- Agents are allowed to start, inspect, and operate Maa and the connected game
  emulator when needed to reproduce issues, diagnose failures, or verify code
  changes. This includes taking screenshots and performing non-destructive
  clicks, swipes, and task runs without requesting separate permission.
- Destructive in-game actions, including selling runes, are allowed only when
  the current user request authorizes them. They must still follow all state
  recognition and confirmation requirements below.
- All game UI clicks and swipes must use human-scale timing rather than the
  fastest possible input rate.
- Add small, bounded random coordinate offsets to click and swipe positions.
  The offset must remain inside the intended control and screen bounds.
- Add modest randomized delays between interactions. Destructive actions such
  as selling still require successful recognition and confirmation; coordinate
  randomization must never replace state validation.
- Treat multi-step UI workflows as explicit state transitions. After every
  click or swipe, recognize and validate the expected resulting state before
  issuing the next input.
- Model optional dialogs and alternate states explicitly. For destructive
  workflows, never infer success from a completed click alone; verify that all
  confirmation dialogs are dismissed and the expected destination screen is
  visible before updating state or continuing.
- If the observed UI state is unknown or ambiguous, stop safely instead of
  performing another click.

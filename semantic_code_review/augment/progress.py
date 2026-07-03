"""Per-hunk progress meter, rendered to stderr in truecolor TTYs.

Shows a single redrawn line:

    overview ✓  hunks 7/24  ●●●●●●●■■····  03s

Each in-flight hunk is one block character coloured by elapsed time —
cyan when fresh, yellow around 15 s, red past ~30 s. A 429 retry sleep
naturally pushes a hunk's elapsed time higher, so the colour reflects
back-pressure without us having to attribute httpx events to the
right concurrent hunk.

Completed hunks fade from green to dim grey over a couple of seconds
but stay visible so the cumulative progress impression is preserved.

Disabled when stderr isn't a TTY, when the terminal doesn't advertise
truecolor, or when the caller passes `enabled=False` (we use this to
suppress the meter under `--verbose`, where it would fight the log
stream).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
import types
from dataclasses import dataclass
from typing import Self, TextIO

# ANSI helpers ---------------------------------------------------------------

ESC = "\x1b["
HIDE_CURSOR = ESC + "?25l"
SHOW_CURSOR = ESC + "?25h"
CLEAR_LINE = ESC + "2K"
RESET = ESC + "0m"
DIM = ESC + "2m"


def _rgb(r: int, g: int, b: int) -> str:
    return f"{ESC}38;2;{r};{g};{b}m"


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


# Heat curve stops. Cool when fresh, hot when stale.
COOL = (96, 200, 240)  # cyan
WARM = (240, 200, 80)  # yellow
HOT = (240, 80, 60)  # red

DONE_FRESH = (100, 220, 120)  # green
DONE_FADED = (90, 90, 90)  # dim grey
FAILED = (220, 80, 60)  # red

HEAT_FULL_SECONDS = 30.0  # ~p99 hunk latency target.
DONE_FADE_SECONDS = 4.0  # how long a closed square stays prominent.


def _heat_color(heat: float) -> tuple[int, int, int]:
    """Heat ∈ [0,1] → RGB along COOL → WARM → HOT."""
    h = max(0.0, min(1.0, heat))
    if h < 0.5:
        return _lerp(COOL, WARM, h * 2)
    return _lerp(WARM, HOT, (h - 0.5) * 2)


def is_truecolor_tty(stream: TextIO | None = None) -> bool:
    """True if `stream` is a TTY *and* the terminal advertises truecolor.

    Uses the de-facto-standard `COLORTERM=truecolor|24bit` signal, plus
    a fallback for terminals known to support it but inconsistent about
    advertising (kitty, alacritty, iTerm).
    """
    s = stream if stream is not None else sys.stderr
    if not getattr(s, "isatty", lambda: False)():
        return False
    if os.environ.get("NO_COLOR"):
        return False
    colorterm = os.environ.get("COLORTERM", "").lower()
    if colorterm in ("truecolor", "24bit"):
        return True
    term = os.environ.get("TERM_PROGRAM", "") or os.environ.get("TERM", "")
    return any(t in term.lower() for t in ("kitty", "alacritty", "iterm"))


@dataclass
class _HunkState:
    started: float | None = None
    finished: float | None = None
    ok: bool = True


class ProgressMeter:
    """Live per-hunk progress meter. Render-only; safe to drive from
    one asyncio loop.
    """

    def __init__(
        self,
        total: int,
        *,
        stream: TextIO | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.total = total
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = is_truecolor_tty(self.stream) if enabled is None else enabled
        self.start_time = time.monotonic()
        self.hunks: list[_HunkState] = [_HunkState() for _ in range(total)]
        self.overview_started: float | None = None
        self.overview_finished: float | None = None
        self.overview_ok: bool = True
        self._task: asyncio.Task | None = None

    # ---- lifecycle -------------------------------------------------------

    async def __aenter__(self) -> Self:
        if self.enabled:
            self.stream.write(HIDE_CURSOR)
            self.stream.flush()
            self._task = asyncio.create_task(self._tick())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: types.TracebackType | None,
    ) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self.enabled:
            # One final paint with current state, then advance to the
            # next line so any subsequent stderr writes don't overwrite.
            self._render()
            self.stream.write("\n" + SHOW_CURSOR)
            self.stream.flush()

    async def _tick(self) -> None:
        try:
            while True:
                self._render()
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            return

    # ---- events ----------------------------------------------------------

    def start_overview(self) -> None:
        self.overview_started = time.monotonic()

    def finish_overview(self, ok: bool = True) -> None:
        self.overview_finished = time.monotonic()
        self.overview_ok = ok

    def start_hunk(self, idx: int) -> None:
        if 0 <= idx < self.total:
            self.hunks[idx].started = time.monotonic()

    def finish_hunk(self, idx: int, ok: bool = True) -> None:
        if 0 <= idx < self.total:
            self.hunks[idx].finished = time.monotonic()
            self.hunks[idx].ok = ok

    # ---- render ----------------------------------------------------------

    def render_line(self) -> str:
        """Build the line as it would be drawn. Public for testing."""
        now = time.monotonic()
        parts: list[str] = []

        # Overview indicator.
        if self.overview_finished is not None:
            mark = "✓" if self.overview_ok else "✗"
            parts.append(f"overview {mark}")
        elif self.overview_started is not None:
            r, g, b = _heat_color((now - self.overview_started) / HEAT_FULL_SECONDS)
            parts.append(f"overview {_rgb(r, g, b)}■{RESET}")
        else:
            parts.append(f"overview {DIM}·{RESET}")

        # Hunks.
        done = sum(1 for h in self.hunks if h.finished is not None)
        squares: list[str] = []
        for h in self.hunks:
            if h.finished is not None:
                if h.ok:
                    fade = max(0.0, min(1.0, (now - h.finished) / DONE_FADE_SECONDS))
                    r, g, b = _lerp(DONE_FRESH, DONE_FADED, fade)
                    squares.append(f"{_rgb(r, g, b)}■{RESET}")
                else:
                    squares.append(f"{_rgb(*FAILED)}■{RESET}")
            elif h.started is not None:
                r, g, b = _heat_color((now - h.started) / HEAT_FULL_SECONDS)
                squares.append(f"{_rgb(r, g, b)}■{RESET}")
            else:
                squares.append(f"{DIM}·{RESET}")
        parts.append(f"hunks {done}/{self.total}  {''.join(squares)}")

        elapsed = int(now - self.start_time)
        parts.append(f"{elapsed:02d}s")
        return "  ".join(parts)

    def _render(self) -> None:
        if not self.enabled:
            return
        # \r + clear-line + line. No trailing newline (we redraw in place).
        self.stream.write(f"\r{CLEAR_LINE}{self.render_line()}")
        self.stream.flush()


__all__ = ["ProgressMeter", "is_truecolor_tty"]

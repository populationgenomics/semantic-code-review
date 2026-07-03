"""Per-hunk progress meter — heat curve, rendering, TTY gating."""

from __future__ import annotations

import io
import time

import pytest

from semantic_code_review.augment.progress import (
    COOL,
    HEAT_FULL_SECONDS,
    HOT,
    ProgressMeter,
    _heat_color,
    is_truecolor_tty,
)


def test_heat_curve_endpoints() -> None:
    assert _heat_color(0.0) == COOL
    assert _heat_color(1.0) == HOT
    # Midpoint is approximately the warm stop (small int rounding noise).
    mid = _heat_color(0.5)
    assert 230 <= mid[0] <= 245  # warm red ~= 240
    assert 190 <= mid[1] <= 210


def test_heat_curve_clamps() -> None:
    assert _heat_color(-1.0) == COOL
    assert _heat_color(2.0) == HOT


def test_disabled_when_not_a_tty() -> None:
    """A buffer isn't a TTY; meter must auto-disable so no ANSI lands."""
    buf = io.StringIO()
    meter = ProgressMeter(total=3, stream=buf)
    assert meter.enabled is False


def test_disabled_under_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeTTY:
        def isatty(self) -> bool:
            return True

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert is_truecolor_tty(FakeTTY()) is False


def test_force_enabled_renders_to_buffer() -> None:
    """Caller can force-enable for testing/CI; render produces ANSI."""
    buf = io.StringIO()
    meter = ProgressMeter(total=3, stream=buf, enabled=True)
    line = meter.render_line()
    # 3 squares — pending dots — plus the overview pending dot.
    assert "0/3" in line
    # Truecolor escape for at least one cell.
    assert "\x1b[38;2;" in line or "\x1b[2m" in line


def test_render_marks_progress() -> None:
    meter = ProgressMeter(total=4, enabled=True, stream=io.StringIO())
    meter.start_overview()
    meter.start_hunk(0)
    meter.start_hunk(1)
    meter.finish_hunk(0, ok=True)
    meter.finish_hunk(1, ok=False)
    meter.finish_overview(ok=True)
    line = meter.render_line()
    assert "2/4" in line  # two completed
    assert "overview ✓" in line


def test_age_pushes_in_flight_color_toward_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hunk that's been open for `HEAT_FULL_SECONDS` should report HOT."""
    meter = ProgressMeter(total=1, enabled=True, stream=io.StringIO())
    fake_now = [time.monotonic()]
    real_monotonic = time.monotonic
    monkeypatch.setattr(
        "semantic_code_review.augment.progress.time.monotonic",
        lambda: fake_now[0],
    )
    meter.start_hunk(0)
    fake_now[0] += HEAT_FULL_SECONDS + 1
    line = meter.render_line()
    # The HOT escape (for full-red) should be present in the line.
    hot_escape = f"\x1b[38;2;{HOT[0]};{HOT[1]};{HOT[2]}m"
    assert hot_escape in line
    # restore (defensive — monkeypatch handles it but be explicit)
    monkeypatch.setattr(
        "semantic_code_review.augment.progress.time.monotonic",
        real_monotonic,
    )


def test_out_of_range_index_is_no_op() -> None:
    meter = ProgressMeter(total=2, enabled=True, stream=io.StringIO())
    meter.start_hunk(99)  # silently ignored
    meter.finish_hunk(-1)  # silently ignored
    line = meter.render_line()
    assert "0/2" in line

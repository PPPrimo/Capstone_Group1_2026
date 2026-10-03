"""
plot_logger.py - standalone data-logging and live-plotting utility.

Usage:
    from server.plot_logger import log_and_plot, flush_plot, set_layout, set_role, set_realtime_plot, plot_json

    set_role("leader")             # logs go to sensitive/log/leader/
    set_realtime_plot(True)        # set False to skip matplotlib (log-only mode)
    set_layout(rows=2, cols=6)     # optional: fix grid before first flush_plot()
    log_and_plot(x, y, "t", "pos", "shoulder_pan_position")  # memory-only append
    flush_plot()                   # call once per loop: redraws & batches disk writes

    plot_json("shoulder_pan_position") # plot full history from .json file (blocking)
"""

import json
import math
import time
from collections import deque
from pathlib import Path

import matplotlib.pyplot as plt

_registry: dict = {}   # name -> {buffer, pending, x_label, y_label}
_fig  = None
_axes: list = []
_layout_rows: int | None = None
_layout_cols: int | None = None
_realtime_plot: bool  = True
_role: str | None     = None

_WINDOW_S        = 60.0   # rolling window shown in live plot
_DISK_FLUSH_S    = 2.0    # batch-flush pending records to disk this often
_REDRAW_S        = 0.05   # cap live redraws at ~20 fps

_last_disk_flush: float = time.monotonic()
_last_redraw:     float = 0.0

_LOG_BASE = Path(__file__).resolve().parent / "sensitive" / "log"
_LOG_DIR  = _LOG_BASE

_COLORS = [
    "#007aff", "#34c759", "#ff3b30", "#ff9500",
    "#af52de", "#5856d6", "#ff6482", "#30b0c7",
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def set_layout(rows: int, cols: int) -> None:
    """Fix the subplot grid. Call before the first flush_plot()."""
    global _layout_rows, _layout_cols
    _layout_rows = max(1, rows)
    _layout_cols = max(1, cols)


def set_role(role: str) -> None:
    """Set the robot role ('leader' or 'follower').
    Logs are stored under sensitive/log/<role>/.
    Call before the first log_and_plot().
    """
    global _role, _LOG_DIR
    _role    = role.lower()
    _LOG_DIR = _LOG_BASE / _role


def set_realtime_plot(enabled: bool) -> None:
    """Enable or disable the live matplotlib window.
    When False, data is still logged to disk but nothing is drawn.
    Call before the first flush_plot().
    """
    global _realtime_plot
    _realtime_plot = enabled


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_figure() -> None:
    global _fig, _axes
    n = len(_registry)
    if n == 0:
        return
    if _layout_rows is not None and _layout_cols is not None:
        rows, cols = _layout_rows, _layout_cols
    else:
        cols = min(n, 3)
        rows = math.ceil(n / cols)
    if _fig is not None:
        plt.close(_fig)
    plt.ion()
    _fig, axs = plt.subplots(rows, cols, figsize=(6 * cols, 4 * rows), squeeze=False)
    _fig.canvas.manager.set_window_title("plot_logger - live view")
    _axes = [axs[r][c] for r in range(rows) for c in range(cols)]
    for ax in _axes[n:]:
        ax.set_visible(False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def log_and_plot(
    x_value,
    y_value,
    x_label: str,
    y_label: str,
    figure_name: str,
    timestamp: float | None = None,
) -> None:
    """Append one point to the in-memory rolling buffer and the pending write queue.
    No disk I/O here — disk writes are batched in flush_plot().
    Call flush_plot() once per loop to render and persist.
    """
    t = timestamp if timestamp is not None else time.time()
    if figure_name not in _registry:
        _registry[figure_name] = {
            "buffer":  deque(),   # rolling _WINDOW_S window
            "pending": [],        # unwritten entries waiting for disk flush
            "x_label": x_label,
            "y_label": y_label,
        }

    entry = {"t": t, "x": x_value, "y": y_value}
    rec   = _registry[figure_name]
    rec["buffer"].append(entry)
    rec["pending"].append(entry)

    # Trim entries older than the rolling window
    cutoff = t - _WINDOW_S
    while rec["buffer"] and rec["buffer"][0]["t"] < cutoff:
        rec["buffer"].popleft()


def flush_plot() -> None:
    """Call once per loop.
    - Appends pending records to <name>.json every _DISK_FLUSH_S seconds.
    - Redraws the live matplotlib window at most every _REDRAW_S seconds.
    The plot is continuous: it always shows the most recent _WINDOW_S seconds.
    """
    global _fig, _axes, _last_disk_flush, _last_redraw

    if not _registry:
        return

    if _fig is None and _realtime_plot:
        _build_figure()

    now = time.monotonic()
    if now - _last_disk_flush >= _DISK_FLUSH_S:
        _last_disk_flush = now
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        for name, rec in _registry.items():
            if not rec["pending"]:
                continue
            fpath = _LOG_DIR / f"{name}.json"
            with fpath.open("a", encoding="utf-8") as fh:
                fh.writelines(
                    json.dumps(e, separators=(",", ":")) + "\n"
                    for e in rec["pending"]
                )
            rec["pending"].clear()

    # --- redraw at capped fps (skipped when realtime plot is disabled) ---
    if not _realtime_plot:
        return
    if now - _last_redraw < _REDRAW_S:
        return
    _last_redraw = now

    names = list(_registry.keys())
    for idx, name in enumerate(names):
        if idx >= len(_axes):
            break
        rec   = _registry[name]
        ax    = _axes[idx]
        color = _COLORS[idx % len(_COLORS)]
        buf   = rec["buffer"]
        xs = [e["x"] for e in buf]
        ys = [e["y"] for e in buf]
        ax.cla()
        if xs:
            ax.plot(xs, ys, color=color, linewidth=1.5)
            ax.fill_between(xs, ys, alpha=0.10, color=color)
        ax.set_xlabel(rec["x_label"])
        ax.set_ylabel(rec["y_label"])
        ax.set_title(name.replace("_", " ").title(), fontsize=10)
        ax.grid(True, color="#e8e8e8")
        ax.spines[["top", "right"]].set_visible(False)

    _fig.tight_layout()
    _fig.canvas.draw()
    _fig.canvas.flush_events()


def plot_json(name: str, path: str | Path | None = None) -> None:
    """Plot the full recorded history for a series from its .json file.
    Blocking — opens a new matplotlib window and calls plt.show().

    Args:
        name: Series name (e.g. 'shoulder_pan_position').
        path: Override file path. Defaults to <LOG_DIR>/<name>.json.
    """
    if path is None:
        path = _LOG_DIR / f"{name}.json"


    entries = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    if not entries:
        print(f"plot_json: no data in {path}")
        return

    xs = [e["x"] for e in entries]
    ys = [e["y"] for e in entries]

    fig, ax = plt.subplots(figsize=(14, 4))
    ax.plot(xs, ys, linewidth=1.2, color="#007aff")
    ax.fill_between(xs, ys, alpha=0.10, color="#007aff")
    ax.set_title(name.replace("_", " ").title(), fontsize=12)
    ax.set_xlabel("x")
    ax.set_ylabel("y")
    ax.grid(True, color="#e8e8e8")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    plt.show()

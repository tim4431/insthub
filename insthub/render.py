"""Results in a form that is cheap for an AI agent to read (used by `insthub mcp`).

A value becomes plain JSON, except for bulk data. An array with more than
`inline` elements, or a long text or byte string, is written to a file and
replaced by a short summary:

    {"array": "float32[1001]", "min": -92.1, "max": -25.0, "file": "/home/me/lab/.insthub/...-sa-trace.npy"}

A large trace therefore costs a few dozen tokens, and the agent loads the
numbers with `numpy.load(file)` only when it needs them. With plot=True the
numeric arrays of the value are also drawn as one small PNG (needs matplotlib).

Plots use the axis conventions of `insthub view`: `t0`/`dt`, or `x0`/`dx` with
`xlabel`/`xunit`, and `unit` for the values. They are read from a stream
frame's meta, or from the fields next to the array in a dict or dataclass.
"""
from __future__ import annotations

import dataclasses
import enum
import io
import os
import re
import tempfile
import threading
import time
from pathlib import Path, PurePath

import numpy as np

from .device import summarize

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_LINES = 8  # traces drawn in one plot
_MAX_IMAGES = 3
_PLOT_POINTS = 4000  # longer traces are drawn as a min/max envelope


def default_folder() -> Path:
    """Where files go: $INSTHUB_DATA, else .insthub in the current directory."""
    env = os.environ.get("INSTHUB_DATA")
    return Path(env).expanduser() if env else Path.cwd() / ".insthub"


@dataclasses.dataclass
class Rendered:
    value: object  #: JSON-ready
    image: bytes | None = None  #: PNG, if a plot was asked for and could be drawn
    image_file: str | None = None
    note: str | None = None  #: why there is no plot although one was asked for


class Renderer:
    """Turns values into JSON plus files.  Safe to use from several threads."""

    def __init__(self, folder: str | Path | None = None, inline: int = 64, inline_text: int = 4000):
        self.folder = (Path(folder).expanduser() if folder else default_folder()).absolute()
        self.inline = int(inline)  #: arrays up to this many elements are returned as lists
        self.inline_text = int(inline_text)  #: strings up to this length are returned as they are
        self._lock = threading.Lock()

    def render(self, value, name: str, *, axis: dict | None = None, plot: bool = False) -> Rendered:
        """`name` (e.g. "sa-trace") goes into the names of the files; `axis` is
        the meta dict of a stream frame, for plotting."""
        walk = _Walk(self, _UNSAFE.sub("_", name).strip("_") or "value")
        out = Rendered(walk.walk(value, (), axis))
        if plot:
            try:
                out.image, out.image_file = self._plot(walk.series, walk.prefix, name)
            except _NoPlot as e:
                out.note = str(e)
        return out

    # -- files -------------------------------------------------------------
    def _target(self, stem: str, suffix: str) -> Path:
        """A new, empty file in the folder (created now, so that no other render takes the name)."""
        with self._lock:
            folder = self._ready_folder()
            path, n = folder / f"{stem}{suffix}", 2
            while True:
                try:
                    path.touch(exist_ok=False)
                    return path
                except FileExistsError:
                    path, n = folder / f"{stem}-{n}{suffix}", n + 1

    def _ready_folder(self) -> Path:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            if os.access(self.folder, os.W_OK):
                return self.folder
        except OSError:
            pass
        # e.g. started with the current directory "/": fall back to a private temporary folder
        uid = os.getuid() if hasattr(os, "getuid") else os.environ.get("USERNAME", "user")
        self.folder = Path(tempfile.gettempdir()) / f"insthub-data-{uid}"
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self.folder

    # -- plots -------------------------------------------------------------
    def _plot(self, series, prefix: str, title: str) -> tuple[bytes, str]:
        try:
            from matplotlib.figure import Figure
        except ImportError:
            raise _NoPlot("no plot: plotting needs matplotlib (pip install matplotlib)") from None
        lines, images = [], []
        for label, a, axis in series:
            if a.dtype.kind == "c":
                a, label = np.abs(a), f"|{label or 'value'}|"
            if a.dtype.kind not in "biuf" or a.size < 2:
                continue
            if a.ndim == 1:
                lines.append((label, a.astype(float), axis))
            elif a.ndim == 2 and min(a.shape) <= _MAX_LINES:  # a few traces, one per row
                rows = a if a.shape[0] <= a.shape[1] else a.T
                lines += [(f"{label}[{i}]", row.astype(float), axis) for i, row in enumerate(rows)]
            elif a.ndim == 2 or (a.ndim == 3 and a.shape[2] in (3, 4)):
                images.append((label, a))
        lines, images = lines[:_MAX_LINES], images[:_MAX_IMAGES]
        if not lines and not images:
            raise _NoPlot("no plot: the result holds no numeric array")

        panels = (1 if lines else 0) + len(images)
        fig = Figure(figsize=(6.4 if panels == 1 else 4.0 * panels, 3.6), dpi=100, layout="constrained")
        axes = list(fig.subplots(1, panels, squeeze=False)[0])
        if lines:
            ax = axes.pop(0)
            for label, y, axis in lines:
                x, xlabel, xunit = _x_axis(axis, len(y))
                ax.plot(*_envelope(x, y), lw=0.8, label=label or None)
            ax.set_xlabel(xlabel)
            if xunit:  # 1.002 GHz, 100 µs
                from matplotlib.ticker import EngFormatter

                ax.xaxis.set_major_formatter(EngFormatter(unit=str(xunit)))
            unit = (lines[0][2] or {}).get("unit") if isinstance(lines[0][2], dict) else None
            if unit:
                ax.set_ylabel(str(unit))
            if len(lines) > 1:
                ax.legend(fontsize="small")
            ax.grid(alpha=0.3)
        for ax, (label, img) in zip(axes, images):
            if img.ndim == 3:
                if img.dtype != np.uint8:
                    img = np.clip(img / max(float(img.max()), 1e-300), 0, 1)
                ax.imshow(img)
            else:
                fig.colorbar(ax.imshow(img, cmap="viridis"), ax=ax, shrink=0.8)
            ax.set_title(label, fontsize="small")
        fig.suptitle(title, fontsize="medium")
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        png = buf.getvalue()
        path = self._target(f"{prefix}-plot", ".png")
        path.write_bytes(png)
        return png, str(path)


class _NoPlot(Exception):
    pass


class _Walk:
    """One rendering: converts the value and collects its arrays for a plot."""

    def __init__(self, renderer: Renderer, name: str):
        self.r = renderer
        now = time.time()
        self.prefix = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}-{name}"
        self.series: list[tuple[str, np.ndarray, dict | None]] = []

    def walk(self, v, path: tuple, axis: dict | None):
        if v is None or isinstance(v, (bool, int, float)):
            return v
        if isinstance(v, str):
            if len(v) <= self.r.inline_text:
                return v
            return {"text": f"str[{len(v)}]", "start": v[:200],
                    "file": self._save(path, ".txt", lambda p: p.write_text(v, encoding="utf-8"))}
        if isinstance(v, np.ndarray):
            return self._array(v, path, axis)
        if isinstance(v, np.floating) and v.dtype.itemsize < 8:
            return float(f"{float(v):.7g}")
        if isinstance(v, np.generic):
            return self.walk(v.item(), path, axis)
        if isinstance(v, (bytes, bytearray, memoryview)):
            v = bytes(v)
            if len(v) <= self.r.inline:
                return repr(v)
            return {"bytes": len(v), "file": self._save(path, ".bin", lambda p: p.write_bytes(v))}
        if isinstance(v, complex):
            return str(v)
        if isinstance(v, enum.Enum):
            return self.walk(v.value, path, axis)
        if isinstance(v, PurePath):
            return str(v)
        if dataclasses.is_dataclass(v) and not isinstance(v, type):
            v = {f.name: getattr(v, f.name) for f in dataclasses.fields(v)}
        if isinstance(v, dict):  # also codec.Record; its fields are the axis of its arrays
            return {str(k): self.walk(x, (*path, str(k)), v) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            if len(v) > self.r.inline and all(isinstance(x, (int, float, np.integer, np.floating)) for x in v):
                return self._array(np.asarray(v), path, axis)
            return [self.walk(x, (*path, str(i)), axis) for i, x in enumerate(v)]
        if isinstance(v, (set, frozenset)):
            try:
                return self.walk(sorted(v), path, axis)
            except TypeError:
                return self.walk(list(v), path, axis)
        return repr(v)

    def _array(self, a: np.ndarray, path: tuple, axis):
        if a.dtype.kind == "O":
            return self.walk(a.tolist(), path, axis)
        self.series.append((".".join(path), a, axis))
        if a.size <= self.r.inline:
            if a.dtype.kind == "f" and a.dtype.itemsize < 8:  # float32: no 0.30000001192092896
                return _map(a.tolist(), lambda x: float(f"{x:.7g}"))
            if a.dtype.kind in "biuf":
                return a.tolist()
            return self.walk(a.tolist(), path, None)  # complex, text, dates: element by element
        file = self._save(path, ".npy", lambda p: np.save(p, a, allow_pickle=False))
        return {"array": summarize(a), **_stats(a), "file": file}

    def _save(self, path: tuple, suffix: str, write) -> str:
        tail = _UNSAFE.sub("_", ".".join(path))
        target = self.r._target(f"{self.prefix}-{tail}" if tail else self.prefix, suffix)
        write(target)
        return str(target)


def _map(x, fn):
    return [_map(i, fn) for i in x] if isinstance(x, list) else fn(x)


def _num(x, itemsize: int) -> float:
    return float(x) if itemsize >= 8 else float(f"{float(x):.7g}")


def _stats(a: np.ndarray) -> dict:
    kind = a.dtype.kind
    if a.size == 0:
        return {}
    if kind in "iu":
        return {"min": int(a.min()), "max": int(a.max())}
    if kind == "b":
        return {"true": int(np.count_nonzero(a))}
    if kind == "c":
        return {"max_abs": _num(np.abs(a).max(), a.dtype.itemsize // 2)}
    if kind == "f":
        finite = np.isfinite(a)
        bad = a.size - int(np.count_nonzero(finite))
        values = a[finite] if bad else a
        out = {"min": _num(values.min(), a.dtype.itemsize), "max": _num(values.max(), a.dtype.itemsize)} \
            if values.size else {}
        if bad:
            out["non_finite"] = bad
        return out
    return {}


def _x_axis(axis, n: int) -> tuple[np.ndarray, str, str | None]:
    """(x values, label, unit) from t0/dt or x0/dx/xlabel/xunit; else the index."""
    if isinstance(axis, dict):
        try:
            if "dt" in axis:
                return float(axis.get("t0") or 0.0) + float(axis["dt"]) * np.arange(n), "time", "s"
            if "dx" in axis:
                return (float(axis.get("x0") or 0.0) + float(axis["dx"]) * np.arange(n),
                        str(axis.get("xlabel") or "x"), axis.get("xunit"))
        except (TypeError, ValueError):
            pass
    return np.arange(n), "index", None


def _envelope(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """A long trace as the min and max of each of a few thousand bins: same look, fast to draw."""
    if len(y) <= _PLOT_POINTS:
        return x, y
    bins = _PLOT_POINTS // 2
    m = len(y) // bins * bins
    yb, xb = y[:m].reshape(bins, -1), x[:m].reshape(bins, -1)
    return np.repeat(xb[:, 0], 2), np.column_stack([yb.min(1), yb.max(1)]).ravel()

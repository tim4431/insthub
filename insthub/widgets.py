"""The look of `insthub gui`: colours, the stylesheet, line icons and the custom
widgets the manager window is made of.  Nothing here knows about servers or
devices; manager.py fills these widgets in.

    Theme, theme(), use()   colour tokens for light and dark; one is current at a time
    stylesheet(), palette() the Qt stylesheet and palette made from the current theme
    icon(), draw_icon()     line icons on a 16 x 16 grid, drawn (no icon theme needed)
    Pill                    a status: a coloured dot and a word, on a tint of the colour
    Tag                     a word in a box beside a title, such as SIM
    Switch                  an on/off toggle
    NumberBox               a spin box for a number with a unit and limits, shown as typed
    NavButton               an entry of the sidebar, with a count beside it
    GridFlow                a layout of equal columns, as many as fit the width
    Tile, TileView          a card painted from a description: icon, title, a preview
                            or a few figures, a status and a caption
    Preview, PreviewView    what a stream looks like, small: a trace, an image, a number
    Toast                   a message floating over the bottom of the window for a while

Colours: the window, cards and text come from the theme.  A status ("good",
"busy", "bad" or "idle") has a fixed colour (STATUS) and always comes with a
word, so colour is never the only signal.  Series colours are the validated categorical palette that
`insthub view` uses too.
"""
from __future__ import annotations

import dataclasses
import math
import os
import tempfile
import time
from pathlib import Path

import numpy as np

from .qt import QtCore, QtGui, QtWidgets, Signal

Qt = QtCore.Qt
STATUS = {"good": "#0ca30c", "busy": "#c98500", "bad": "#d03b3b", "idle": "#898781",  #: in both themes alike
          "paused": "#7563e0"}  # held on purpose: neither fine nor broken


@dataclasses.dataclass(frozen=True)
class Theme:
    name: str
    dark: bool
    window: str  #: behind everything
    sidebar: str
    surface: str  #: cards and tiles
    inset: str  #: wells inside a card: previews, code
    border: str  #: around cards
    strong: str  #: around controls
    hover: str
    ink: str  #: text
    ink2: str  #: secondary text
    muted: str  #: tertiary text and hints
    accent: str
    accent_hover: str
    accent_soft: str  #: selection, the current page
    on_accent: str
    series: tuple


LIGHT = Theme("light", False, window="#f6f6f4", sidebar="#eeeeeb", surface="#ffffff", inset="#f4f4f1",
              border="#e4e3dd", strong="#cfcec6", hover="#ecece8", ink="#1b1b1a", ink2="#52514e",
              muted="#77766f", accent="#2a78d6", accent_hover="#256abf", accent_soft="#e2edfb", on_accent="#ffffff",
              series=("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"))
DARK = Theme("dark", True, window="#141413", sidebar="#1b1b1a", surface="#212120", inset="#191918",
             border="#2f2f2d", strong="#43423f", hover="#2b2b29", ink="#f0efec", ink2="#c3c2b7",
             muted="#8f8d86", accent="#3987e5", accent_hover="#5598e7", accent_soft="#1e3552", on_accent="#ffffff",
             series=("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"))

_current = LIGHT
_desktop_dark: bool | None = None


def desktop_dark() -> bool:
    """Whether the desktop asks for dark windows (Qt 6.5+ knows; else its palette says)."""
    global _desktop_dark
    app = QtWidgets.QApplication.instance()
    scheme = getattr(app.styleHints(), "colorScheme", None) if app is not None else None
    if scheme is not None:
        value = scheme()
        if value == Qt.ColorScheme.Dark:
            return True
        if value == Qt.ColorScheme.Light:
            return False
    if _desktop_dark is None:  # the palette as it was before the window set its own
        window = app.palette().color(QtGui.QPalette.ColorRole.Window) if app is not None else None
        _desktop_dark = window is not None and window.lightness() < 128
    return _desktop_dark


def theme(choice: str = "system") -> Theme:
    """"light", "dark", or "system": as the desktop."""
    if choice == "dark":
        return DARK
    if choice == "light":
        return LIGHT
    return DARK if desktop_dark() else LIGHT


def use(t: Theme) -> None:
    global _current
    _current = t


def current() -> Theme:
    return _current


def color(value: str, alpha: float = 1.0) -> QtGui.QColor:
    c = QtGui.QColor(value)
    c.setAlphaF(alpha)
    return c


def muted(text: str) -> str:
    """Rich text in the muted colour of the current theme."""
    return f"<span style='color:{_current.muted}'>{text}</span>"


def _keyboard(reason) -> bool:
    """Whether focus came from the keyboard: only then is a focus ring drawn (a click shows itself)."""
    return reason in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                      Qt.FocusReason.ShortcutFocusReason)


class _Ring:
    """Mixin: remembers whether the widget has focus from the keyboard, as `self.ring`."""

    ring = False

    def focusInEvent(self, event) -> None:
        self.ring = _keyboard(event.reason())
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self.ring = False
        super().focusOutEvent(event)


def _flags(*flags) -> int:
    """Alignment and text flags as the int QPainter.drawText takes (enums differ between bindings)."""
    return sum(f.value if hasattr(f, "value") else int(f) for f in flags)


_LEFT = (Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignVCenter)
_RIGHT = (Qt.AlignmentFlag.AlignRight, Qt.AlignmentFlag.AlignVCenter)


def scaled(font: QtGui.QFont, factor: float, weight=None) -> QtGui.QFont:
    font = QtGui.QFont(font)
    if font.pointSizeF() > 0:
        font.setPointSizeF(font.pointSizeF() * factor)
    else:
        font.setPixelSize(max(6, round(font.pixelSize() * factor)))
    if weight is not None:
        font.setWeight(weight)
    return font


_SEMIBOLD = QtGui.QFont.Weight.DemiBold
_MEDIUM = QtGui.QFont.Weight.Medium


# ---------------------------------------------------------------- the stylesheet
def stylesheet(t: Theme, pt: float) -> str:
    """The stylesheet of the window (its dialogs inherit it).  `pt`: the base font size."""
    small, h1, h2 = pt * 0.9, pt * 1.7, pt * 1.15
    return f"""
QMainWindow, QDialog {{ background: {t.window}; }}
QWidget {{ color: {t.ink}; }}
QWidget#sidebar {{ background: {t.sidebar}; border-right: 1px solid {t.border}; }}
QWidget#page, QWidget#content {{ background: {t.window}; }}
QScrollArea {{ background: transparent; border: none; }}
QLabel {{ background: transparent; }}
QLabel[role="h1"] {{ font-size: {h1:.1f}pt; font-weight: 600; }}
QLabel[role="h2"] {{ font-size: {h2:.1f}pt; font-weight: 600; }}
QLabel[role="brand"] {{ font-size: {h2:.1f}pt; font-weight: 700; }}
QLabel[role="muted"] {{ color: {t.muted}; }}
QLabel[role="small"] {{ color: {t.muted}; font-size: {small:.1f}pt; }}
QLabel[role="label"] {{ color: {t.ink2}; }}
QLabel[role="error"] {{ color: {STATUS['bad']}; }}
QLabel#toast {{ background: {t.ink}; color: {t.surface}; border-radius: 9px; padding: 9px 16px; }}
QFrame[card="true"] {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 12px; }}
QFrame[role="rule"] {{ background: {t.border}; border: none; }}
QPushButton {{ background: {t.surface}; border: 1px solid {t.strong}; border-radius: 8px; padding: 6px 14px; }}
QPushButton:hover {{ background: {t.hover}; }}
QPushButton:pressed {{ background: {t.border}; }}
QPushButton:focus {{ border-color: {t.accent}; }}
QPushButton:disabled {{ color: {t.muted}; background: {t.window}; border-color: {t.border}; }}
QPushButton[kind="primary"] {{ background: {t.accent}; border-color: {t.accent}; color: {t.on_accent};
                               font-weight: 600; }}
QPushButton[kind="primary"]:hover {{ background: {t.accent_hover}; border-color: {t.accent_hover}; }}
QPushButton[kind="primary"]:disabled {{ background: {t.window}; border-color: {t.border}; color: {t.muted}; }}
QPushButton[kind="ghost"] {{ background: transparent; border-color: transparent; }}
QPushButton[kind="ghost"]:hover {{ background: {t.hover}; }}
QPushButton[kind="ghost"]:disabled {{ background: transparent; border-color: transparent; }}
QPushButton[kind="link"] {{ background: transparent; border: none; color: {t.accent}; padding: 2px 0px; }}
QPushButton[kind="link"]:hover {{ color: {t.accent_hover}; }}
QPushButton[kind="section"] {{ background: transparent; border: none; padding: 0px; font-size: {h2:.1f}pt;
                               font-weight: 600; text-align: left; }}
QPushButton[kind="section"]:hover {{ color: {t.accent}; }}
QPushButton[kind="danger"] {{ color: {STATUS['bad']}; }}
QPushButton::menu-indicator {{ image: none; width: 0px; }}
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {t.surface}; border: 1px solid {t.strong}; border-radius: 8px; padding: 5px 8px;
    selection-background-color: {t.accent}; selection-color: {t.on_accent}; }}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QAbstractSpinBox:focus {{ border-color: {t.accent}; }}
QLineEdit:read-only {{ background: {t.inset}; }}
QLineEdit:disabled, QComboBox:disabled, QAbstractSpinBox:disabled {{ color: {t.muted}; background: {t.inset}; }}
QPlainTextEdit[role="code"], QLineEdit[role="code"] {{ font-family: monospace; background: {t.inset}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QAbstractSpinBox {{ padding-right: 24px; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ subcontrol-origin: border; width: 22px;
    border: none; background: transparent; }}
QAbstractSpinBox::up-button {{ subcontrol-position: top right; border-top-right-radius: 8px; }}
QAbstractSpinBox::down-button {{ subcontrol-position: bottom right; border-bottom-right-radius: 8px; }}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{ background: {t.hover}; }}
QAbstractSpinBox::up-arrow {{ image: url("{_asset("up", t.ink2)}"); width: 10px; height: 10px; }}
QAbstractSpinBox::down-arrow {{ image: url("{_asset("down", t.ink2)}"); width: 10px; height: 10px; }}
QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::up-arrow:off {{ image: url("{_asset("up", t.muted)}"); }}
QAbstractSpinBox::down-arrow:disabled, QAbstractSpinBox::down-arrow:off {{
    image: url("{_asset("down", t.muted)}"); }}
QComboBox::down-arrow {{ image: url("{_asset("down", t.ink2)}"); width: 12px; height: 12px; }}
QComboBox::down-arrow:disabled {{ image: url("{_asset("down", t.muted)}"); }}
QComboBox QAbstractItemView {{ background: {t.surface}; border: 1px solid {t.border}; padding: 4px;
    selection-background-color: {t.accent_soft}; selection-color: {t.ink}; outline: 0; }}
QTableView, QListView {{ background: {t.surface}; border: none; outline: 0; gridline-color: {t.border};
    selection-background-color: {t.accent_soft}; selection-color: {t.ink}; }}
QTableView::item {{ padding: 4px 10px; border-bottom: 1px solid {t.border}; }}
QTableView::item:selected {{ background: {t.accent_soft}; color: {t.ink}; }}
QListView[card="true"] {{ border: 1px solid {t.border}; border-radius: 10px; padding: 4px; }}
QListView::item {{ padding: 6px 8px; border-radius: 6px; }}
QListView::item:selected {{ background: {t.accent_soft}; color: {t.ink}; }}
QHeaderView {{ background: {t.surface}; border: none; }}
QHeaderView::section {{ background: {t.surface}; color: {t.muted}; border: none;
    border-bottom: 1px solid {t.border}; padding: 7px 10px; font-weight: 600; font-size: {small:.1f}pt; }}
QTableCornerButton::section {{ background: {t.surface}; border: none; }}
QTabWidget::pane {{ border: none; }}
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{ background: transparent; border: none; border-bottom: 2px solid transparent; padding: 8px 2px;
    margin-right: 22px; color: {t.ink2}; }}
QTabBar::tab:hover {{ color: {t.ink}; }}
QTabBar::tab:selected {{ color: {t.ink}; border-bottom-color: {t.accent}; }}
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t.strong}; border-radius: 4px; min-height: 28px; }}
QScrollBar::handle:horizontal {{ background: {t.strong}; border-radius: 4px; min-width: 28px; }}
QScrollBar::handle:hover {{ background: {t.muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; height: 0px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QMenu {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 10px; padding: 5px; }}
QMenu::item {{ padding: 6px 22px 6px 30px; border-radius: 6px; }}
QMenu::item:selected {{ background: {t.accent_soft}; color: {t.ink}; }}
QMenu::item:disabled {{ color: {t.muted}; }}
QMenu::separator {{ height: 1px; background: {t.border}; margin: 5px 8px; }}
QMenu::icon {{ padding-left: 10px; }}
QToolTip {{ background: {t.ink}; color: {t.surface}; border: none; padding: 5px 8px; }}
QCheckBox {{ spacing: 8px; }}
"""


def palette(t: Theme) -> QtGui.QPalette:
    """An application palette to match, for what the stylesheet does not reach."""
    p = QtGui.QPalette()
    role = QtGui.QPalette.ColorRole
    for r, value in ((role.Window, t.window), (role.WindowText, t.ink), (role.Base, t.surface),
                     (role.AlternateBase, t.inset), (role.Text, t.ink), (role.Button, t.surface),
                     (role.ButtonText, t.ink), (role.Highlight, t.accent), (role.HighlightedText, t.on_accent),
                     (role.ToolTipBase, t.ink), (role.ToolTipText, t.surface), (role.Link, t.accent),
                     (role.PlaceholderText, t.muted), (role.Light, t.surface), (role.Midlight, t.border),
                     (role.Mid, t.strong), (role.Dark, t.strong), (role.Shadow, "#000000")):
        p.setColor(r, QtGui.QColor(value))
    for r in (role.Text, role.WindowText, role.ButtonText):
        p.setColor(QtGui.QPalette.ColorGroup.Disabled, r, QtGui.QColor(t.muted))
    return p


def _asset(name: str, ink: str) -> str:
    """A stylesheet needs an image file: an icon drawn into one, at twice its size (for sharp screens)."""
    folder = Path(tempfile.gettempdir()) / f"insthub-gui-{os.getuid() if hasattr(os, 'getuid') else 'user'}"
    path = folder / f"{name}-{ink.lstrip('#')}.png"
    if not path.is_file():
        folder.mkdir(mode=0o700, exist_ok=True)
        pix = QtGui.QPixmap(24, 24)
        pix.fill(Qt.GlobalColor.transparent)
        p = QtGui.QPainter(pix)
        draw_icon(p, QtCore.QRectF(0, 0, 24, 24), name, ink, width=2.0)
        p.end()
        pix.save(str(path))
    return path.as_posix()


# ---------------------------------------------------------------- icons
def _pt(x, y) -> QtCore.QPointF:
    return QtCore.QPointF(x, y)


def _line(p, *xy) -> None:
    p.drawPolyline(QtGui.QPolygonF([_pt(xy[i], xy[i + 1]) for i in range(0, len(xy), 2)]))


def _spot(p, x, y, r) -> None:
    ink = p.pen().color()
    p.save()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(ink)
    p.drawEllipse(_pt(x, y), r, r)
    p.restore()


def _wave(p, x0, x1, y, amplitude, periods) -> None:
    n = 40
    _line(p, *[v for i in range(n + 1) for v in (x0 + (x1 - x0) * i / n,
                                                 y - amplitude * math.sin(2 * math.pi * periods * i / n))])


def _rect(p, x, y, w, h, r) -> None:
    p.drawRoundedRect(QtCore.QRectF(x, y, w, h), r, r)


def _i_refresh(p) -> None:
    p.drawArc(QtCore.QRectF(2.5, 2.5, 11, 11), 40 * 16, 290 * 16)
    _line(p, 13.4, 1.9, 12.7, 5.0, 9.6, 4.4)


def _i_sliders(p) -> None:
    for y, knob in ((4, 10.5), (8, 5.5), (12, 9.5)):
        p.drawLine(_pt(2, y), _pt(knob - 2, y))
        p.drawLine(_pt(knob + 2, y), _pt(14, y))
        p.drawEllipse(_pt(knob, y), 2, 2)


def _i_sparkle(p) -> None:
    path = QtGui.QPainterPath(_pt(8, 1.8))
    path.cubicTo(8.4, 5.6, 10.4, 7.6, 14.2, 8)
    path.cubicTo(10.4, 8.4, 8.4, 10.4, 8, 14.2)
    path.cubicTo(7.6, 10.4, 5.6, 8.4, 1.8, 8)
    path.cubicTo(5.6, 7.6, 7.6, 5.6, 8, 1.8)
    p.drawPath(path)


def _i_chip(p) -> None:
    _rect(p, 4, 4, 8, 8, 1.6)
    for v in (6, 8, 10):
        for a, b in (((v, 1.6), (v, 4)), ((v, 12), (v, 14.4)), ((1.6, v), (4, v)), ((12, v), (14.4, v))):
            p.drawLine(_pt(*a), _pt(*b))


def _i_globe(p) -> None:
    p.drawEllipse(_pt(8, 8), 6, 6)
    p.drawEllipse(_pt(8, 8), 2.6, 6)
    p.drawLine(_pt(2, 8), _pt(14, 8))


def _i_bolt(p) -> None:
    p.drawPolygon(QtGui.QPolygonF([_pt(9, 1.5), _pt(3.5, 9), _pt(7.5, 9), _pt(6.5, 14.5), _pt(12.5, 7),
                                   _pt(8.5, 7)]))


_ICONS = {
    "devices": lambda p: [_rect(p, x, y, 5, 5, 1.3) for x, y in ((2, 2), (9, 2), (2, 9), (9, 9))],
    "servers": lambda p: (_rect(p, 2, 2.5, 12, 4.5, 1.5), _rect(p, 2, 9, 12, 4.5, 1.5),
                          _spot(p, 4.8, 4.75, 0.9), _spot(p, 4.8, 11.25, 0.9)),
    "agents": _i_sparkle,
    "settings": _i_sliders,
    "plus": lambda p: (p.drawLine(_pt(8, 3), _pt(8, 13)), p.drawLine(_pt(3, 8), _pt(13, 8))),
    "refresh": _i_refresh,
    "back": lambda p: _line(p, 10, 3, 5, 8, 10, 13),
    "forward": lambda p: _line(p, 6, 3, 11, 8, 6, 13),
    "down": lambda p: _line(p, 4, 6, 8, 10, 12, 6),
    "up": lambda p: _line(p, 4, 10, 8, 6, 12, 10),
    "live": lambda p: _line(p, 1.5, 8.5, 4.5, 8.5, 6.5, 3.5, 9.5, 12.5, 11.5, 8.5, 14.5, 8.5),
    "more": lambda p: [_spot(p, x, 8, 1.35) for x in (3.5, 8, 12.5)],
    "search": lambda p: (p.drawEllipse(_pt(7, 7), 4.5, 4.5), p.drawLine(_pt(10.4, 10.4), _pt(14, 14))),
    "power": lambda p: (p.drawArc(QtCore.QRectF(2.5, 3, 11, 11), 125 * 16, 290 * 16),
                        p.drawLine(_pt(8, 1.8), _pt(8, 7.5))),
    "release": lambda p: (_line(p, 6.5, 2.5, 3, 2.5, 3, 13.5, 6.5, 13.5), p.drawLine(_pt(7, 8), _pt(14, 8)),
                          _line(p, 11, 5, 14, 8, 11, 11)),
    "logs": lambda p: (_rect(p, 1.75, 2.75, 12.5, 10.5, 2), _line(p, 4.5, 6, 6.5, 8, 4.5, 10),
                       p.drawLine(_pt(8, 10), _pt(11.5, 10))),
    "window": lambda p: (_rect(p, 1.75, 2.75, 12.5, 10.5, 2), p.drawLine(_pt(1.75, 6), _pt(14.25, 6))),
    "stop": lambda p: _rect(p, 3.5, 3.5, 9, 9, 1.5),
    "pause": lambda p: (_rect(p, 4, 3, 2.8, 10, 1), _rect(p, 9.2, 3, 2.8, 10, 1)),
    "edit": lambda p: (_line(p, 3, 13, 3.5, 10, 11, 2.5, 13.5, 5, 6, 12.5, 3, 13), p.drawLine(_pt(9.5, 4), _pt(12, 6.5))),
    "start": lambda p: p.drawPolygon(QtGui.QPolygonF([_pt(5, 3), _pt(13, 8), _pt(5, 13)])),
    "trash": lambda p: (p.drawLine(_pt(2.5, 4.5), _pt(13.5, 4.5)), _line(p, 6, 4.5, 6.5, 2.5, 9.5, 2.5, 10, 4.5),
                        _line(p, 4, 4.5, 4.8, 13.5, 11.2, 13.5, 12, 4.5)),
    "file": lambda p: (_line(p, 9.5, 1.75, 3.5, 1.75, 3.5, 14.25, 12.5, 14.25, 12.5, 4.75, 9.5, 1.75),
                       _line(p, 9.5, 1.75, 9.5, 4.75, 12.5, 4.75)),
    "folder": lambda p: _line(p, 1.75, 3.5, 6, 3.5, 7.5, 5.5, 14.25, 5.5, 14.25, 13, 1.75, 13, 1.75, 3.5),
    "copy": lambda p: (_rect(p, 5.5, 5.5, 8.5, 8.5, 1.5), _line(p, 10.5, 3.2, 10.5, 2, 2, 2, 2, 10.5, 3.2, 10.5)),
    "check": lambda p: _line(p, 3, 8.5, 6.5, 12, 13, 4.5),
    "close": lambda p: (p.drawLine(_pt(4, 4), _pt(12, 12)), p.drawLine(_pt(12, 4), _pt(4, 12))),
    "globe": _i_globe,
    "scope": lambda p: (_rect(p, 1.75, 2.75, 12.5, 10.5, 2), _wave(p, 3.8, 12.2, 8, 2.4, 1)),
    "camera": lambda p: (_rect(p, 1.75, 4.5, 12.5, 9, 2), p.drawEllipse(_pt(8, 9), 2.6, 2.6),
                         _line(p, 5.5, 4.5, 6.5, 2.5, 9.5, 2.5, 10.5, 4.5)),
    "synth": lambda p: _wave(p, 1.5, 14.5, 8, 4.5, 1.5),
    "spectrum": lambda p: [p.drawLine(_pt(x, 13.5), _pt(x, 13.5 - h)) for x, h in ((3, 4), (6, 9), (9, 6), (12, 11))],
    "bolt": _i_bolt,
    "chip": _i_chip,
}


def draw_icon(p: QtGui.QPainter, rect: QtCore.QRectF, name: str, ink, width: float = 1.5) -> None:
    """Draw a line icon into `rect` (kept square, centred)."""
    p.save()
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    side = min(rect.width(), rect.height())
    p.translate(rect.x() + (rect.width() - side) / 2, rect.y() + (rect.height() - side) / 2)
    p.scale(side / 16, side / 16)
    pen = QtGui.QPen(QtGui.QColor(ink))
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    _ICONS[name](p)
    p.restore()


class _Engine(QtGui.QIconEngine):
    """Paints an icon at whatever size and pixel ratio is asked for; `ink` None: the theme's secondary ink."""

    def __init__(self, name: str, ink: str | None):
        super().__init__()
        self._name, self._ink = name, ink

    def paint(self, painter, rect, mode, state) -> None:
        ink = QtGui.QColor(self._ink or _current.ink2)
        if mode == QtGui.QIcon.Mode.Disabled:
            ink.setAlphaF(0.35)
        draw_icon(painter, QtCore.QRectF(rect), self._name, ink)

    def pixmap(self, size, mode, state):
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(self, size, mode, state, scale):
        pix = QtGui.QPixmap(max(1, round(size.width() * scale)), max(1, round(size.height() * scale)))
        pix.fill(Qt.GlobalColor.transparent)
        pix.setDevicePixelRatio(scale)
        p = QtGui.QPainter(pix)
        self.paint(p, QtCore.QRect(0, 0, size.width(), size.height()), mode, state)
        p.end()
        return pix

    def clone(self):
        return _Engine(self._name, self._ink)


def icon(name: str, ink: str | None = None) -> QtGui.QIcon:
    return QtGui.QIcon(_Engine(name, ink))


# ---------------------------------------------------------------- small widgets
def label(text: str = "", role: str | None = None, wrap: bool = False, select: bool = False) -> QtWidgets.QLabel:
    widget = QtWidgets.QLabel(text)
    if role:
        widget.setProperty("role", role)
    widget.setWordWrap(wrap)
    if select:
        widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def button(text: str, icon_name: str | None = None, kind: str | None = None, tip: str = "",
           slot=None) -> QtWidgets.QPushButton:
    widget = QtWidgets.QPushButton(text)
    if icon_name:
        widget.setIcon(icon(icon_name, _current.on_accent if kind == "primary" else None))
    if kind:
        widget.setProperty("kind", kind)
    if tip:
        widget.setToolTip(tip)
    if slot is not None:
        widget.clicked.connect(lambda *_: slot())
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    return widget


def card(layout_cls=QtWidgets.QVBoxLayout, margins=(18, 16, 18, 16), spacing: int = 10):
    """A rounded card: (the frame, its layout)."""
    frame = QtWidgets.QFrame()
    frame.setProperty("card", True)
    layout = layout_cls(frame)
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)
    return frame, layout


def scroll_page(margins=(28, 4, 28, 28), spacing: int = 18):
    """A scrolling page body: (the scroll area, the layout of its content)."""
    area = QtWidgets.QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    content = QtWidgets.QWidget()
    content.setObjectName("content")
    layout = QtWidgets.QVBoxLayout(content)
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)
    area.setWidget(content)
    return area, layout


def pill_width(font: QtGui.QFont, text: str) -> float:
    return 25 + QtGui.QFontMetricsF(font).horizontalAdvance(text) + 10


def paint_pill(p: QtGui.QPainter, x: float, y: float, status: str, text: str, font: QtGui.QFont,
               height: float = 22) -> float:
    """A status pill ("good", "busy", "bad" or "idle") with its left edge at x; returns its width."""
    t = _current
    width = pill_width(font, text)
    rect = QtCore.QRectF(x, y, width, height)
    p.save()
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color(t.strong, 0.45) if status == "idle" else color(STATUS[status], 0.2 if t.dark else 0.13))
    p.drawRoundedRect(rect, height / 2, height / 2)
    centre = _pt(x + 12, y + height / 2)
    if status == "idle":  # a ring: nothing to report
        p.setPen(QtGui.QPen(QtGui.QColor(t.muted), 1.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(centre, 3.2, 3.2)
    elif status == "paused":  # two bars, like a pause button
        p.setBrush(QtGui.QColor(STATUS[status]))
        for dx in (-3.2, 1.0):
            p.drawRoundedRect(QtCore.QRectF(centre.x() + dx, centre.y() - 4, 2.2, 8), 1, 1)
    else:
        p.setBrush(QtGui.QColor(STATUS[status]))
        p.drawEllipse(centre, 3.6, 3.6)
    p.setPen(QtGui.QColor(t.ink))
    p.setFont(font)
    p.drawText(QtCore.QRectF(x + 21, y, width - 25, height), _flags(*_LEFT), text)
    p.restore()
    return width


class Pill(QtWidgets.QWidget):
    """A status in a word: a coloured dot on a tint of its colour."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._status, self._text = "idle", ""
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)

    def set(self, status: str, text: str) -> None:
        if (status, text) != (self._status, self._text):
            self._status, self._text = status, text
            self.updateGeometry()
            self.update()

    def text(self) -> str:
        return self._text

    def status(self) -> str:
        return self._status

    def _font(self) -> QtGui.QFont:
        return scaled(self.font(), 0.95, _MEDIUM)

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(math.ceil(pill_width(self._font(), self._text)) + 2, 24)

    def paintEvent(self, event) -> None:
        p = QtGui.QPainter(self)
        paint_pill(p, 1, (self.height() - 22) / 2, self._status, self._text, self._font())
        p.end()


def tag_width(font: QtGui.QFont, text: str) -> float:
    return QtGui.QFontMetricsF(font).horizontalAdvance(text) + 12


def paint_tag(p: QtGui.QPainter, x: float, y: float, text: str, font: QtGui.QFont, height: float = 18) -> float:
    """A word in a box (SIM), in the accent colour, with its left edge at x; returns its width.  It
    says what something is, not how it is, so it never takes a status colour."""
    t = _current
    width = tag_width(font, text)
    rect = QtCore.QRectF(x, y, width, height)
    p.save()
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    p.setPen(QtGui.QPen(color(t.accent, 0.55), 1))
    p.setBrush(QtGui.QColor(t.accent_soft))
    p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
    p.setPen(QtGui.QColor(t.accent))
    p.setFont(font)
    p.drawText(rect, _flags(Qt.AlignmentFlag.AlignCenter), text)
    p.restore()
    return width


class Tag(QtWidgets.QWidget):
    """A word in a box beside a title (see paint_tag)."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._text = text
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)

    def text(self) -> str:
        return self._text

    def _font(self) -> QtGui.QFont:
        return scaled(self.font(), 0.8, _SEMIBOLD)

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(math.ceil(tag_width(self._font(), self._text)) + 2, 22)

    def paintEvent(self, event) -> None:
        p = QtGui.QPainter(self)
        paint_tag(p, 1, (self.height() - 18) / 2, self._text, self._font())
        p.end()


class Switch(_Ring, QtWidgets.QAbstractButton):
    """An on/off toggle (a checkable button)."""

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(38, 22)

    def set_quietly(self, on: bool) -> None:
        """Show a state without emitting toggled (it came from the device, not from a click)."""
        blocked = self.blockSignals(True)
        self.setChecked(on)
        self.blockSignals(blocked)

    def paintEvent(self, event) -> None:
        t = _current
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        track = QtCore.QRectF(2, (self.height() - 18) / 2, 34, 18)
        on = self.isChecked()
        alpha = 1.0 if self.isEnabled() else 0.45
        if self.ring:
            p.setPen(QtGui.QPen(color(t.accent, 0.5), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(track.adjusted(-1.5, -1.5, 1.5, 1.5), 10.5, 10.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color(t.accent if on else t.strong, alpha))
        p.drawRoundedRect(track, 9, 9)
        knob = QtCore.QRectF(track.right() - 16 if on else track.left() + 2, track.top() + 2, 14, 14)
        p.setBrush(color("#ffffff", alpha))
        p.drawEllipse(knob)
        p.end()


class NumberBox(QtWidgets.QDoubleSpinBox):
    """A spin box for a number with a unit and limits (set_meta), which shows it as it is typed
    (2500, 0.001) rather than with a fixed count of decimals.  Enter emits `submitted`."""

    submitted = Signal()
    DECIMALS = 9

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setLocale(QtCore.QLocale.c())  # typed as on the command line: a point, never a comma
        self.setDecimals(self.DECIMALS)
        self.set_meta({})

    def set_meta(self, meta: dict) -> None:
        """Its limits, step and unit, from a value's metadata (see device.META_KEYS).  Without a
        step, each click changes the digit before the last one shown."""
        def number(key, default):
            value = meta.get(key)
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default

        self.setRange(number("min", -1e12), number("max", 1e12))
        step = number("step", None)
        self.setSingleStep(step or 1.0)
        self.setStepType(QtWidgets.QAbstractSpinBox.StepType.DefaultStepType if step else
                         QtWidgets.QAbstractSpinBox.StepType.AdaptiveDecimalStepType)
        self.setSuffix(f" {meta['unit']}" if meta.get("unit") else "")

    def fits(self, value: float) -> bool:
        """Whether it can show `value` as it is: within its limits, and not too small for its decimals."""
        return self.minimum() <= value <= self.maximum() and math.isclose(round(value, self.DECIMALS), value,
                                                                          rel_tol=1e-9)

    def textFromValue(self, value: float) -> str:
        text = f"{value:.{self.DECIMALS}f}".rstrip("0").rstrip(".")
        return "0" if text == "-0" else text

    def keyPressEvent(self, event) -> None:
        super().keyPressEvent(event)
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.submitted.emit()


class NavButton(_Ring, QtWidgets.QAbstractButton):
    """An entry of the sidebar: an icon, a name and a count; `alert` puts a red dot before the count."""

    def __init__(self, text: str, icon_name: str, parent=None):
        super().__init__(parent)
        self.setText(text)
        self._icon, self._count, self._alert = icon_name, "", False
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)

    def set_count(self, count: str, alert: bool = False) -> None:
        if (count, alert) != (self._count, self._alert):
            self._count, self._alert = count, alert
            self.update()

    def count(self) -> str:
        return self._count

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(180, 36)

    def paintEvent(self, event) -> None:
        t = _current
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = QtCore.QRectF(self.rect()).adjusted(0, 1, 0, -1)
        on = self.isChecked()
        if on or self.underMouse() or self.ring:
            p.setPen(QtGui.QPen(QtGui.QColor(t.accent), 1.2) if self.ring else Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor(t.accent_soft if on else t.hover))
            p.drawRoundedRect(rect, 8, 8)
        draw_icon(p, QtCore.QRectF(12, rect.center().y() - 8, 16, 16), self._icon, t.accent if on else t.ink2)
        p.setFont(scaled(self.font(), 1.0, _SEMIBOLD if on else _MEDIUM))
        p.setPen(QtGui.QColor(t.ink))
        p.drawText(QtCore.QRectF(38, rect.top(), rect.width() - 80, rect.height()), _flags(*_LEFT), self.text())
        if self._count:
            small = scaled(self.font(), 0.9, _MEDIUM)
            p.setFont(small)
            right = rect.width() - 12
            width = QtGui.QFontMetricsF(small).horizontalAdvance(self._count)
            p.setPen(QtGui.QColor(t.ink if self._alert else t.muted))
            p.drawText(QtCore.QRectF(right - width, rect.top(), width, rect.height()), _flags(*_RIGHT), self._count)
            if self._alert:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QtGui.QColor(STATUS["bad"]))
                p.drawEllipse(_pt(right - width - 8, rect.center().y()), 3.5, 3.5)
        p.end()


class GridFlow(QtWidgets.QLayout):
    """Children in equal columns, as many as fit at `min_width` each, filled row by row.
    Hidden children take no place.  Its height follows from its width."""

    def __init__(self, parent=None, min_width: int = 250, gap: int = 14):
        super().__init__(parent)
        self._items: list = []
        self.min_width, self.gap = min_width, gap
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QtCore.QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect) -> None:
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QtCore.QSize:
        return self.minimumSize()

    def minimumSize(self) -> QtCore.QSize:
        shown = [i for i in self._items if not i.isEmpty()]
        height = max((i.sizeHint().height() for i in shown), default=0)
        return QtCore.QSize(self.min_width, height)

    def columns(self, width: int) -> int:
        return max(1, (width + self.gap) // (self.min_width + self.gap))

    def _arrange(self, rect, apply: bool) -> int:
        shown = [i for i in self._items if not i.isEmpty()]
        if not shown:
            return 0
        cols = self.columns(rect.width())
        width = (rect.width() - (cols - 1) * self.gap) / cols
        y, row_height = rect.y(), 0
        for index, item in enumerate(shown):
            col = index % cols
            if col == 0 and index:
                y += row_height + self.gap
                row_height = 0
            height = item.sizeHint().height()
            if apply:
                left = rect.x() + round(col * (width + self.gap))
                right = rect.x() + round(col * (width + self.gap) + width)
                item.setGeometry(QtCore.QRect(left, y, right - left, height))
            row_height = max(row_height, height)
        return y + row_height - rect.y()

    def set_order(self, widgets: list) -> None:
        """Put the children in this order (the ones listed; others are removed)."""
        current = [i.widget() for i in self._items]
        if current == widgets:
            return
        while self._items:
            self.takeAt(0)
        for widget in widgets:
            self.addWidget(widget)
        self.invalidate()


# ---------------------------------------------------------------- previews and tiles
@dataclasses.dataclass(eq=False)
class Preview:
    """What a stream looks like at a glance.  `kind`: "trace" (data: a list of 1-D float arrays),
    "image" (a QImage), "number" (a float; history: recent values, oldest first) or "text"."""

    kind: str
    data: object
    unit: str = ""
    stream: str = ""
    time: float = 0.0  #: when the frame was published
    history: tuple = ()

    def age(self) -> float:
        return time.time() - self.time if self.time else 0.0


def format_value(value) -> str:
    """A value for a figure: 1000, 0.001, 2.5e9, on/off, a short list."""
    if isinstance(value, (bool, np.bool_)):
        return "on" if value else "off"
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    if isinstance(value, (float, np.floating)):
        v = float(value)
        if not math.isfinite(v):
            return str(v)
        if v == int(v) and abs(v) < 1e6:
            return str(int(v))
        text = f"{v:.4g}"
        mantissa, _, exponent = text.partition("e")
        return f"{mantissa}e{int(exponent)}" if exponent else text
    if isinstance(value, (list, tuple)):
        return " × ".join(format_value(v) for v in value) if all(isinstance(v, int) for v in value) \
            else ", ".join(format_value(v) for v in value)
    return str(value)


def _paint_traces(p, rect: QtCore.QRectF, series: list, t: Theme) -> None:
    finite = [a[np.isfinite(a)] for a in series]
    finite = [a for a in finite if a.size]
    if not finite:
        return
    lo, hi = min(float(a.min()) for a in finite), max(float(a.max()) for a in finite)
    if hi - lo < 1e-30:
        lo, hi = lo - 1, hi + 1
    for index, a in enumerate(series):
        n = len(a)
        if n < 2:
            continue
        xs = rect.left() + rect.width() * np.arange(n) / (n - 1)
        ys = rect.bottom() - (a - lo) / (hi - lo) * rect.height()
        ok = np.isfinite(ys)
        points = [_pt(float(x), float(y)) for x, y in zip(xs[ok], ys[ok], strict=True)]
        pen = QtGui.QPen(QtGui.QColor(t.series[index % len(t.series)]), 1.4)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawPolyline(QtGui.QPolygonF(points))


def _paint_number(p, rect: QtCore.QRectF, preview: Preview, font: QtGui.QFont, t: Theme) -> None:
    big = scaled(font, 1.9, _SEMIBOLD)
    p.setFont(big)
    p.setPen(QtGui.QColor(t.ink))
    text = format_value(preview.data)
    width = QtGui.QFontMetricsF(big).horizontalAdvance(text)
    has_history = len(preview.history) > 1
    left = QtCore.QRectF(rect.left() + 4, rect.top(), rect.width() * (0.5 if has_history else 1.0), rect.height())
    p.drawText(left, _flags(*_LEFT), text)
    if preview.unit:
        p.setFont(scaled(font, 1.0))
        p.setPen(QtGui.QColor(t.muted))
        p.drawText(left.adjusted(width + 6, 0, 0, 0), _flags(*_LEFT), preview.unit)
    if has_history:
        spark = QtCore.QRectF(rect.left() + rect.width() * 0.55, rect.top() + rect.height() * 0.2,
                              rect.width() * 0.45 - 4, rect.height() * 0.6)
        values = np.asarray(preview.history, dtype=np.float64)
        _paint_traces(p, spark, [values], dataclasses.replace(t, series=(t.accent,)))
        finite = values[np.isfinite(values)]
        if finite.size:
            lo, hi = float(finite.min()), float(finite.max())
            span = hi - lo if hi - lo > 1e-30 else 2.0
            y = spark.bottom() - (values[-1] - (lo if hi - lo > 1e-30 else lo - 1)) / span * spark.height()
            if math.isfinite(y):
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QtGui.QColor(t.accent))
                p.drawEllipse(_pt(spark.right(), y), 2.6, 2.6)


def paint_preview(p: QtGui.QPainter, rect: QtCore.QRectF, preview: Preview, font: QtGui.QFont,
                  stale: bool = False) -> None:
    t = _current
    p.save()
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QtGui.QColor(t.inset))
    p.drawRoundedRect(rect, 8, 8)
    if stale:
        p.setOpacity(0.4)
    inner = rect.adjusted(8, 8, -8, -8)
    if preview.kind == "trace":
        _paint_traces(p, inner, preview.data, t)
    elif preview.kind == "image":
        image = preview.data
        if image.width() and image.height():
            scale = min(rect.width() / image.width(), rect.height() / image.height())
            w, h = image.width() * scale, image.height() * scale
            target = QtCore.QRectF(rect.center().x() - w / 2, rect.center().y() - h / 2, w, h)
            clip = QtGui.QPainterPath()
            clip.addRoundedRect(rect, 8, 8)
            p.setClipPath(clip)
            p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(target, image)
    elif preview.kind == "number":
        _paint_number(p, inner, preview, font, t)
    else:
        p.setFont(font)
        p.setPen(QtGui.QColor(t.ink2))
        p.drawText(inner, _flags(Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignVCenter,
                                 Qt.TextFlag.TextWordWrap), str(preview.data))
    p.restore()


@dataclasses.dataclass
class TileView:
    """What a tile shows.  Its body is the preview if there is one, else the message, else the figures."""

    title: str
    subtitle: str = ""
    icon: str = "chip"
    status: str = "idle"  #: "good", "busy", "bad" or "idle"
    word: str = ""
    preview: Preview | None = None
    stale: bool = False  #: the preview is old: drawn faded
    message: str = ""
    figures: tuple = ()  #: ((label, value), ...): up to six
    caption: str = ""
    tip: str = ""
    badge: str = ""  #: a word in a box beside the title, such as SIM


class Tile(_Ring, QtWidgets.QWidget):
    """A card painted from a TileView.  A click (or Enter) emits `clicked`; the
    "..." in its corner, a right click or the menu key emit `menu_requested`."""

    clicked = Signal()
    menu_requested = Signal(object)  # the global position (QPoint) for the menu
    HEIGHT = 186
    PAD = 14

    def __init__(self, view: TileView | None = None, parent=None, menu: bool = True):
        super().__init__(parent)
        self.view = view or TileView("")
        self._menu = menu
        self._pressed = False
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setFixedHeight(self.HEIGHT)

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(280, self.HEIGHT)

    def set_view(self, view: TileView) -> None:
        if view != self.view:
            self.view = view
            self.setToolTip(view.tip)
            self.setAccessibleName(f"{view.title}: {view.word}")
            self.update()

    def _more(self) -> QtCore.QRectF:
        return QtCore.QRectF(self.width() - self.PAD - 26, self.PAD - 2, 26, 26)

    def mousePressEvent(self, event) -> None:
        self._pressed = event.button() == Qt.MouseButton.LeftButton
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        pos = event.position() if hasattr(event, "position") else QtCore.QPointF(event.pos())
        if self._pressed and event.button() == Qt.MouseButton.LeftButton and self.rect().contains(pos.toPoint()):
            if self._menu and self._more().contains(pos):
                self.menu_requested.emit(self.mapToGlobal(self._more().bottomLeft().toPoint()))
            else:
                self.clicked.emit()
        self._pressed = False

    def mouseMoveEvent(self, event) -> None:
        self.update()  # the hover ring of the "..."

    def contextMenuEvent(self, event) -> None:
        self.menu_requested.emit(event.globalPos())

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
        elif event.key() == Qt.Key.Key_Menu and self._menu:
            self.menu_requested.emit(self.mapToGlobal(self.rect().center()))
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        t, v, pad = _current, self.view, self.PAD
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        hover, focus = self.underMouse(), self.ring
        frame = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QtGui.QPen(QtGui.QColor(t.accent if focus else t.strong if hover else t.border), 1.5 if focus else 1))
        p.setBrush(QtGui.QColor(t.surface))
        p.drawRoundedRect(frame, 12, 12)

        badge = QtCore.QRectF(pad, pad, 36, 36)  # the icon, on a tint of the status colour
        ink = t.ink2 if v.status == "idle" else STATUS[v.status]
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color(t.strong, 0.35) if v.status == "idle" else color(ink, 0.2 if t.dark else 0.12))
        p.drawRoundedRect(badge, 9, 9)
        draw_icon(p, badge.adjusted(9, 9, -9, -9), v.icon, ink)

        left, right = badge.right() + 12, self.width() - pad - (30 if self._menu else 0)
        title = scaled(self.font(), 1.08, _SEMIBOLD)
        small = scaled(self.font(), 0.92)
        fm, fs = QtGui.QFontMetricsF(title), QtGui.QFontMetricsF(small)
        top = badge.center().y() - (fm.height() + fs.height()) / 2
        room = right - left
        if v.badge:  # right after the title, which gives up room for it if it must
            tag_font = scaled(self.font(), 0.78, _SEMIBOLD)
            room -= tag_width(tag_font, v.badge) + 8
            shown = fm.elidedText(v.title, Qt.TextElideMode.ElideRight, room)
            paint_tag(p, left + fm.horizontalAdvance(shown) + 8, top + (fm.height() - 18) / 2, v.badge, tag_font)
        p.setFont(title)
        p.setPen(QtGui.QColor(t.ink))
        p.drawText(QtCore.QRectF(left, top, room, fm.height()), _flags(*_LEFT),
                   fm.elidedText(v.title, Qt.TextElideMode.ElideRight, room))
        p.setFont(small)
        p.setPen(QtGui.QColor(t.muted))
        p.drawText(QtCore.QRectF(left, top + fm.height(), right - left, fs.height()), _flags(*_LEFT),
                   fs.elidedText(v.subtitle, Qt.TextElideMode.ElideRight, right - left))
        if self._menu and (hover or focus):
            more = self._more()
            pos = self.mapFromGlobal(QtGui.QCursor.pos())
            if more.contains(QtCore.QPointF(pos)):
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QtGui.QColor(t.hover))
                p.drawRoundedRect(more, 7, 7)
            draw_icon(p, more.adjusted(5, 5, -5, -5), "more", t.ink2)

        body = QtCore.QRectF(pad, badge.bottom() + 12, self.width() - 2 * pad, 78)
        if v.preview is not None:
            paint_preview(p, body, v.preview, small, v.stale)
        elif v.message:
            p.setFont(small)
            p.setPen(QtGui.QColor(t.ink2))
            p.drawText(body.adjusted(0, 4, 0, 0), _flags(Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignTop,
                                                        Qt.TextFlag.TextWordWrap), v.message)
        elif v.figures:
            self._figures(p, body, v.figures, small)

        y = self.height() - pad - 22
        word = scaled(self.font(), 0.92, _MEDIUM)
        width = paint_pill(p, pad, y, v.status, v.word, word)
        if v.caption:
            space = self.width() - 2 * pad - width - 12
            p.setFont(small)
            p.setPen(QtGui.QColor(t.muted))
            p.drawText(QtCore.QRectF(self.width() - pad - space, y, space, 22), _flags(*_RIGHT),
                       fs.elidedText(v.caption, Qt.TextElideMode.ElideLeft, space))
        p.end()

    def _figures(self, p, body: QtCore.QRectF, figures: tuple, small: QtGui.QFont) -> None:
        t = _current
        figures = figures[:6]
        cols = min(3, len(figures))
        rows = math.ceil(len(figures) / cols)
        value_font = scaled(self.font(), 1.12, _SEMIBOLD)
        fl, fv = QtGui.QFontMetricsF(small), QtGui.QFontMetricsF(value_font)
        cell_w = body.width() / cols
        cell_h = fl.height() + fv.height() + 4
        top = body.top() + (body.height() - rows * cell_h) / 2 if rows == 1 else body.top()
        for index, (name, value) in enumerate(figures):
            x = body.left() + (index % cols) * cell_w
            y = top + (index // cols) * (cell_h + (body.height() - 2 * cell_h if rows == 2 else 0))
            width = cell_w - 6
            p.setFont(small)
            p.setPen(QtGui.QColor(t.muted))
            p.drawText(QtCore.QRectF(x, y, width, fl.height()), _flags(*_LEFT),
                       fl.elidedText(name, Qt.TextElideMode.ElideRight, width))
            p.setFont(value_font)
            p.setPen(QtGui.QColor(t.ink))
            p.drawText(QtCore.QRectF(x, y + fl.height() + 2, width, fv.height()), _flags(*_LEFT),
                       fv.elidedText(value, Qt.TextElideMode.ElideRight, width))


class AddTile(_Ring, QtWidgets.QWidget):
    """A tile-sized place to add something (a device to a server): a dashed outline, a plus,
    a label and a line of detail.  A click (or Enter) emits `clicked`."""

    clicked = Signal()

    def __init__(self, text: str, detail: str = "", parent=None):
        super().__init__(parent)
        self.text, self.detail = text, detail
        self._pressed = False
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setFixedHeight(Tile.HEIGHT)
        self.setAccessibleName(text)

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(280, Tile.HEIGHT)

    def set_detail(self, detail: str) -> None:
        if detail != self.detail:
            self.detail = detail
            self.update()

    def mousePressEvent(self, event) -> None:
        self._pressed = event.button() == Qt.MouseButton.LeftButton
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        pos = event.position() if hasattr(event, "position") else QtCore.QPointF(event.pos())
        if self._pressed and event.button() == Qt.MouseButton.LeftButton and self.rect().contains(pos.toPoint()):
            self.clicked.emit()
        self._pressed = False

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        t = _current
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        hover, focus = self.underMouse(), self.ring
        pen = QtGui.QPen(QtGui.QColor(t.accent if (hover or focus) else t.strong), 1.5)
        pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(QtGui.QColor(t.hover) if hover else Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1), 12, 12)
        ink = t.accent if (hover or focus) else t.ink2
        label = scaled(self.font(), 1.02, _MEDIUM)
        small = scaled(self.font(), 0.92)
        fl, fs = QtGui.QFontMetricsF(label), QtGui.QFontMetricsF(small)
        size = 28
        block = size + 10 + fl.height() + (fs.height() + 2 if self.detail else 0)
        top = (self.height() - block) / 2
        draw_icon(p, QtCore.QRectF((self.width() - size) / 2, top, size, size), "plus", ink)
        y = top + size + 10
        width = self.width() - 28
        p.setFont(label)
        p.setPen(QtGui.QColor(ink))
        p.drawText(QtCore.QRectF(14, y, width, fl.height()),
                   _flags(Qt.AlignmentFlag.AlignHCenter, Qt.AlignmentFlag.AlignVCenter), self.text)
        if self.detail:
            p.setFont(small)
            p.setPen(QtGui.QColor(t.muted))
            p.drawText(QtCore.QRectF(14, y + fl.height() + 2, width, fs.height()),
                       _flags(Qt.AlignmentFlag.AlignHCenter, Qt.AlignmentFlag.AlignVCenter),
                       fs.elidedText(self.detail, Qt.TextElideMode.ElideMiddle, width))
        p.end()


class PreviewView(QtWidgets.QWidget):
    """A preview on its own, larger: the device page shows one."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.preview: Preview | None = None
        self.stale = False
        self.setMinimumHeight(120)

    def set_preview(self, preview: Preview | None, stale: bool = False) -> None:
        if (preview, stale) != (self.preview, self.stale):
            self.preview, self.stale = preview, stale
            self.update()

    def paintEvent(self, event) -> None:
        if self.preview is None:
            return
        p = QtGui.QPainter(self)
        paint_preview(p, QtCore.QRectF(self.rect()), self.preview, scaled(self.font(), 1.0), self.stale)
        p.end()


class Toast(QtWidgets.QLabel):
    """A short message at the bottom of its parent, gone after a while (or kept: ms=0)."""

    def __init__(self, parent: QtWidgets.QWidget):
        super().__init__(parent)
        self.setObjectName("toast")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setWordWrap(False)
        self.hide()
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        parent.installEventFilter(self)

    def show_message(self, text: str, ms: int = 5000) -> None:
        metrics = self.fontMetrics()
        limit = max(200, self.parentWidget().width() - 120)
        self.setText(metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, limit))
        self.setToolTip(text)
        self.adjustSize()
        self._place()
        self.show()
        self.raise_()
        if ms:
            self._timer.start(ms)
        else:
            self._timer.stop()

    def _place(self) -> None:
        parent = self.parentWidget()
        self.move((parent.width() - self.width()) // 2, parent.height() - self.height() - 22)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QtCore.QEvent.Type.Resize and self.isVisible():
            self._place()
        return False

"""The desktop launcher of the manager window: `insthub gui --install`.

Writes a freedesktop.org launcher, so that insthub appears in the
applications menu (Activities on GNOME, from where it can be pinned to the
dock) and, if there is a desktop folder, as an icon on the desktop:

    ~/.local/share/applications/insthub.desktop       the menu entry
    ~/.local/share/icons/hicolor/scalable/apps/insthub.svg
    ~/Desktop/insthub.desktop                          the desktop icon

The launcher runs `python -m insthub gui` with the Python interpreter that
installed it, like `insthub service install` does.  "Start at login" in the
tray menu writes ~/.config/autostart/insthub.desktop, which runs
`insthub gui --tray`.  Linux desktops only.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .errors import BusError

APP_ID = "insthub"  #: the .desktop file name; the manager window carries it too, so the dock matches them up
ICON = Path(__file__).with_name("insthub.svg")


def data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def desktop_dir() -> Path | None:
    """The user's desktop folder (its name depends on the language), or None if there is none."""
    path = None
    if shutil.which("xdg-user-dir"):
        try:
            out = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        if out.strip() and Path(out.strip()) != Path.home():  # it answers $HOME when there is no desktop folder
            path = Path(out.strip())
    path = path or Path.home() / "Desktop"
    return path if path.is_dir() else None


def _quote(arg: str) -> str:
    """One argument of an Exec= line (the desktop entry specification's quoting)."""
    for ch in ("\\", '"', "`", "$"):
        arg = arg.replace(ch, "\\" + ch)
    return '"' + arg.replace("%", "%%") + '"'


def entry_text(icon: str | Path, python: str | None = None, args: tuple = ()) -> str:
    command = " ".join(_quote(part) for part in (python or sys.executable, "-m", "insthub", "gui", *args))
    return f"""[Desktop Entry]
Type=Application
Version=1.5
Name=insthub
GenericName=Instrument manager
Comment=See the instrument servers on this computer and how each device is connected
Exec={command}
Icon={icon}
Terminal=false
Categories=Science;Engineering;
Keywords=instrument;lab;device;server;oscilloscope;
StartupNotify=true
StartupWMClass={APP_ID}
"""


def install(desktop: bool = True, python: str | None = None) -> list[Path]:
    """Write the launcher (and the desktop icon); returns the files written.  Again to update them."""
    if not sys.platform.startswith("linux"):
        raise BusError("`insthub gui --install` writes a Linux desktop launcher; elsewhere, make a shortcut to "
                       f"`{sys.executable} -m insthub gui` by hand")
    icon = data_dir() / "icons" / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg"
    icon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ICON, icon)
    text = entry_text(icon, python)
    menu = data_dir() / "applications" / f"{APP_ID}.desktop"
    menu.parent.mkdir(parents=True, exist_ok=True)
    menu.write_text(text)
    written = [menu, icon]
    folder = desktop_dir() if desktop else None
    if folder is not None:
        shortcut = folder / f"{APP_ID}.desktop"
        shortcut.write_text(text)
        shortcut.chmod(0o755)  # GNOME only launches desktop files that are executable ...
        if shutil.which("gio"):  # ... and marked as trusted
            subprocess.run(["gio", "set", str(shortcut), "metadata::trusted", "true"], capture_output=True)
        written.append(shortcut)
    return written


def autostart_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / f"{APP_ID}.desktop"


def autostart() -> bool:
    """Whether the window starts (in the tray) when you log in."""
    return autostart_path().is_file()


def set_autostart(on: bool, python: str | None = None) -> None:
    path = autostart_path()
    if on:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(entry_text(ICON, python, args=("--tray",)) + "X-GNOME-Autostart-enabled=true\n")
    else:
        path.unlink(missing_ok=True)


def uninstall() -> list[Path]:
    """Remove what `install()` and `set_autostart()` wrote; returns the files removed."""
    removed = []
    folder = desktop_dir()
    for path in (autostart_path(), data_dir() / "applications" / f"{APP_ID}.desktop",
                 data_dir() / "icons" / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg",
                 *([folder / f"{APP_ID}.desktop"] if folder else [])):
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed

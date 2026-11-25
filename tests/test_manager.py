"""The manager window (`insthub gui`) and its launcher, run without a display."""
import os
import shutil
import subprocess
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("insthub.qt", exc_type=ImportError)  # needs a Qt binding

import insthub  # noqa: E402
from insthub import cli, launcher, manager, qt, service, sim  # noqa: E402

Checked, Unchecked = qt.QtCore.Qt.CheckState.Checked, qt.QtCore.Qt.CheckState.Unchecked


def spin(seconds, until=lambda: False):
    app = qt.application()
    end = time.monotonic() + seconds
    while time.monotonic() < end and not until():
        app.processEvents()
        time.sleep(0.01)
    return until()


@pytest.fixture
def window():
    qt.application()
    w = manager.Manager(refresh_ms=150)
    yield w
    w.close()
    spin(0.1)


def word(w, *key) -> str:
    """The status word shown in the tree for a server or device."""
    item = w._items.get(key)
    return item.text(1) if item is not None else ""


def select(w, *key):
    w.tree.setCurrentItem(w._items[key])
    spin(0.05)


def test_servers_devices_and_their_pages(window):
    with insthub.Server("t", [sim.SimScope(), sim.Loopback()], tcp=0) as server:
        assert spin(5, lambda: word(window, "device", "local:t", "scope") == "connected")
        assert word(window, "server", "local:t") == "running"
        select(window, "device", "local:t", "scope")
        page = window.device_page
        assert window.pages.currentWidget() is page and page.title.text() == "scope"
        assert "SimScope" in page.subtitle.text() and "Connected since" in page.instrument.text()
        assert spin(3, lambda: "Unix socket" in page.via.text())          # (in-process for a moment at start)
        assert spin(3, lambda: page.streams.rowCount() == 2)                 # ch1, ch2 ("state" has its own tab)
        assert page.streams.item(0, 0).text() == "ch1"
        assert page.tasks.item(0, 0).text() == "_acquire" and page.tasks.item(0, 2).checkState() == Checked
        assert page.state.rowCount() == 3                                    # timebase, signal_hz, live
        assert not page.connect_now.isEnabled() and page.release.isEnabled()

        select(window, "server", "local:t")
        spage = window.server_page
        assert spin(3, lambda: "ipc://" in spage.reach.text() and "tcp://" in spage.reach.text())
        assert "started by hand" in spage.service.text()
        assert spage.stop.isHidden()                                         # never offers to stop itself
        assert spage.latency.text().endswith("ms")
        server.stop()
        assert spin(5, lambda: ("server", "local:t") not in window._items)   # gone from the tree


def test_release_reopen_and_periodic_tasks(window):
    with insthub.Server("t", [sim.SimScope(), sim.Loopback()]) as server:
        assert spin(5, lambda: word(window, "device", "local:t", "loopback") == "connected")
        select(window, "device", "local:t", "loopback")
        window.device_page.release.click()
        assert spin(5, lambda: word(window, "device", "local:t", "loopback") == "released")
        assert server.devices["loopback"].connected is False
        assert "Released" in window.device_page.instrument.text() and window.device_page.connect_now.isEnabled()
        window.device_page.connect_now.click()
        assert spin(5, lambda: word(window, "device", "local:t", "loopback") == "connected")

        select(window, "device", "local:t", "scope")
        assert spin(3, lambda: window.device_page.tasks.rowCount() == 1)
        window.device_page.tasks.item(0, 2).setCheckState(Unchecked)       # what a click does
        assert spin(5, lambda: not server.devices["scope"].tasks["_acquire"].enabled)
        assert spin(3, lambda: window.device_page.tasks.item(0, 2).text() == "off")
        window.device_page.tasks.item(0, 2).setCheckState(Checked)
        assert spin(5, lambda: server.devices["scope"].tasks["_acquire"].enabled)


def test_other_programs_and_stopping_a_server(window, spawned, monkeypatch):
    spawned.start()
    watcher = subprocess.Popen([sys.executable, "-m", "insthub", "watch", "scope", "ch1"], env=os.environ.copy(),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert spin(10, lambda: word(window, "device", "local:child", "scope") == "connected")
        select(window, "device", "local:child", "scope")
        if sys.platform.startswith("linux"):                                 # programs are named from /proc
            assert spin(10, lambda: "insthub watch scope ch1 (ch1)" in window.device_page.watchers.text())
        select(window, "server", "local:child")
        assert spin(3, lambda: window.server_page.clients.rowCount() == 1)
        assert window.server_page.clients.item(0, 3).text() == "scope/ch1"
        assert not window.server_page.stop.isHidden()
        asked = []
        monkeypatch.setattr(window, "confirm", lambda *a: asked.append(a) or True)
        window.server_page.stop.click()
        spawned.proc.wait(10)
        assert asked and spawned.proc.returncode == 0                        # SIGTERM: a clean shutdown
        assert spin(5, lambda: ("server", "local:child") not in window._items)
    finally:
        watcher.terminate()
        watcher.wait(5)


def test_services_and_remote_devices(monkeypatch, tmp_path):
    (tmp_path / "devices.toml").write_text('[devices]\nawg = "tcp://127.0.0.1:1"\n')
    monkeypatch.setenv("INSTHUB_DEVICES", str(tmp_path / "devices.toml"))
    unit = {"enabled": True, "command": '"python" "-m" "insthub" "serve" "t.toml"', "unit": "u"}
    monkeypatch.setattr(service, "units", lambda: [
        {**unit, "name": "t", "state": "active", "sub": "running", "pid": os.getpid()},
        {**unit, "name": "lab", "state": "failed", "sub": "failed", "pid": None}])
    calls = []
    for action in ("start", "stop", "restart", "remove"):
        monkeypatch.setattr(service, action, lambda name, action=action: calls.append((action, name)))
    qt.application()
    with insthub.Server("t", [sim.Loopback()]):
        window = manager.Manager(refresh_ms=150)
        monkeypatch.setattr(window, "confirm", lambda *a: True)
        try:
            assert spin(5, lambda: word(window, "server", "service:lab") == "failed"
                        and word(window, "server", "remote:tcp://127.0.0.1:1") == "unreachable"
                        and word(window, "device", "local:t", "loopback") == "connected")
            assert word(window, "device", "remote:tcp://127.0.0.1:1", "awg") == "unknown"
            assert window.servers["local:t"].service["name"] == "t"          # matched by pid
            assert window.add_service.isEnabled()

            select(window, "server", "local:t")
            page = window.server_page
            assert "insthub-t · active · starts at login" in page.service.text()
            assert page.start.isHidden() and not page.restart.isHidden()
            page.restart.click()
            page.stop.click()                                                 # a service is stopped by systemd
            select(window, "server", "service:lab")
            assert "Logs shows why" in page.state.text() and not page.start.isHidden()
            page.start.click()
            assert spin(5, lambda: len(calls) == 3)
            assert calls == [("restart", "t"), ("stop", "t"), ("start", "lab")]

            select(window, "server", "remote:tcp://127.0.0.1:1")
            assert "Does not answer" in page.state.text() and "devices.toml" in page.subtitle.text()
        finally:
            window.close()
            spin(0.1)


class Flaky(insthub.Device):
    """Connected until its cable is pulled; then it stays down."""

    reconnect_interval = 60.0

    def open(self):
        if getattr(self, "broken", False):
            raise OSError("cable pulled")

    @insthub.command
    def pull_cable(self):
        self.broken = True
        raise OSError("cable pulled")          # a link error: the session is dropped


@pytest.fixture
def settings(tmp_path):
    """Qt settings (tray choices) in a temporary folder, not the user's."""
    settings = qt.QtCore.QSettings
    settings.setPath(settings.Format.NativeFormat, settings.Scope.UserScope, str(tmp_path / "settings"))
    yield
    settings.setPath(settings.Format.NativeFormat, settings.Scope.UserScope,
                     str(qt.QtCore.QStandardPaths.writableLocation(qt.QtCore.QStandardPaths.StandardLocation.ConfigLocation)))


def test_tray_keeps_watching_and_tells_when_something_breaks(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(manager, "HIDDEN_REFRESH_MS", 200)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    qt.application()
    with insthub.Server("t", [Flaky("flaky")]) as server:
        window = manager.Manager(refresh_ms=150, tray=True)
        told = []
        monkeypatch.setattr(window.tray, "showMessage", lambda title, text, *a: told.append((title, text)))
        try:
            window.show()
            assert spin(5, lambda: word(window, "device", "local:t", "flaky") == "connected")
            menu = [a.text() for a in window.tray.menu.actions()]
            assert "t: running · 1 device" in menu and "Quit (servers keep running)" in menu
            assert window.tray.toolTip().startswith("insthub: 1 server, 1 device")

            window.close()                                               # to the tray, not gone
            assert not window.isVisible() and not window._closed
            assert told and told[0][0] == "insthub is still here"
            window.close()
            assert len(told) == 1                                        # said once only

            with pytest.raises(OSError):
                server.device("flaky").pull_cable()
            assert spin(5, lambda: len(told) == 2)                       # noticed while hidden
            assert told[1] == ("flaky on t: not connected", "Not connected: OSError: cable pulled")
            assert "flaky on t: not connected" in window.tray.toolTip()
            window.tray.messageClicked.emit()                            # clicking it shows the device
            assert window.isVisible() and window._selected() == ("device", "local:t", "flaky")

            login = next(a for a in window.tray.menu.actions() if a.text().startswith("Start at login"))
            login.trigger()
            assert '"gui" "--tray"' in launcher.autostart_path().read_text() and launcher.autostart()
            login.trigger()
            assert not launcher.autostart()
            notify = next(a for a in window.tray.menu.actions() if a.text().startswith("Notify me"))
            notify.trigger()
            assert window.tray.notify is False

            window.quit()
            assert window._closed and not window.tray.isVisible()
        finally:
            window._shutdown()
            spin(0.1)


def test_one_window_at_a_time():
    qt.application()
    first = manager._single_instance(tray_only=False)
    try:
        assert first is not None and first.isListening()
        assert manager._single_instance(tray_only=False) is None         # the second start knocks and leaves
        assert spin(3, lambda: first.hasPendingConnections())
    finally:
        first.close()


def test_command_names():
    name = manager.command_name
    assert name(["/venv/bin/python", "-u", "experiment.py", "--fast"]) == "experiment.py --fast"
    assert name(["/venv/bin/python", "/venv/bin/insthub", "view", "scope"]) == "insthub view scope"
    assert name(["python3", "-m", "insthub", "-a", "ipc:///run/x.sock", "view", "scope"]) == "insthub view scope"
    assert name(["python", "-m", "ipykernel_launcher", "-f", "kernel.json"]) == "Jupyter kernel"
    assert name(["/opt/vendor/bin/scopeapp"]) == "scopeapp"


def test_find_python(tmp_path):
    (tmp_path / "rig" / ".venv" / "bin").mkdir(parents=True)
    python = tmp_path / "rig" / ".venv" / "bin" / "python"
    python.write_text("")
    (tmp_path / "rig" / "servers").mkdir()
    assert manager.find_python(tmp_path / "rig" / "servers" / "lab.toml") == str(python)   # the folder above
    assert manager.find_python(tmp_path / "other.toml") == sys.executable


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux desktop launchers")
def test_launcher(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "Desktop").mkdir()
    monkeypatch.setattr(launcher, "desktop_dir", lambda: tmp_path / "Desktop")
    assert cli.main(["gui", "--install"]) == 0
    menu = tmp_path / "data" / "applications" / "insthub.desktop"
    shortcut = tmp_path / "Desktop" / "insthub.desktop"
    icon = tmp_path / "data" / "icons" / "hicolor" / "scalable" / "apps" / "insthub.svg"
    assert menu.is_file() and shortcut.is_file() and icon.read_bytes() == launcher.ICON.read_bytes()
    text = menu.read_text()
    assert f'Exec="{sys.executable}" "-m" "insthub" "gui"' in text and f"Icon={icon}" in text
    assert os.access(shortcut, os.X_OK)
    if shutil.which("desktop-file-validate"):
        result = subprocess.run(["desktop-file-validate", str(menu)], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    assert "applications menu" in capsys.readouterr().out
    launcher.set_autostart(True)
    autostart = launcher.autostart_path()
    assert autostart == tmp_path / "config" / "autostart" / "insthub.desktop"
    if shutil.which("desktop-file-validate"):
        result = subprocess.run(["desktop-file-validate", str(autostart)], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    assert cli.main(["gui", "--uninstall"]) == 0
    assert not menu.exists() and not shortcut.exists() and not icon.exists() and not autostart.exists()

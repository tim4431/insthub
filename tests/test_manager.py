"""The manager window (`insthub gui`) and its launcher, run without a display."""
import json
import os
import shutil
import subprocess
import sys
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("insthub.qt", exc_type=ImportError)  # needs a Qt binding

import insthub  # noqa: E402
from insthub import cli, config, forms, inventory, launcher, manager, pages, qt, registry, service, sim  # noqa: E402
from insthub import widgets as ui  # noqa: E402


def spin(seconds, until=lambda: False):
    app = qt.application()
    end = time.monotonic() + seconds
    while time.monotonic() < end and not until():
        app.processEvents()
        time.sleep(0.01)
    return until()


@pytest.fixture(autouse=True)
def settings(tmp_path):
    """Qt settings (theme, tray choices) in a temporary folder, not the user's."""
    settings = qt.QtCore.QSettings
    settings.setPath(settings.Format.NativeFormat, settings.Scope.UserScope, str(tmp_path / "settings"))
    yield
    settings.setPath(settings.Format.NativeFormat, settings.Scope.UserScope,
                     str(qt.QtCore.QStandardPaths.writableLocation(qt.QtCore.QStandardPaths.StandardLocation.ConfigLocation)))


@pytest.fixture
def window():
    qt.application()
    w = manager.Manager(refresh_ms=150)
    yield w
    w.close()
    spin(0.1)


def word(w, *key) -> str:
    """The status word of a server (its card) or a device (its tile)."""
    s = w.servers.get(key[1])
    if s is None:
        return ""
    if key[0] == "server":
        return pages.server_view(s).word
    return pages.device_view(s, key[2]).word if key[2] in s.device_names() else ""


def test_tiles_and_the_pages_of_a_device_and_a_server(window):
    with insthub.Server("t", [sim.SimScope(), sim.Loopback()], tcp=0) as server:
        assert spin(5, lambda: word(window, "device", "local:t", "scope") == "connected")
        assert word(window, "server", "local:t") == "running"
        assert spin(3, lambda: ("local:t", "scope") in window.devices_page.tiles)
        tile = window.devices_page.tiles["local:t", "scope"]
        assert spin(3, lambda: tile.view.word == "connected")
        assert tile.view.subtitle == "SimScope · t" and tile.view.icon == "scope"
        assert tile.view.figures == (("Timebase", "0.001"), ("Signal hz", "1000"), ("Live", "on"))  # its state
        assert window.devices_page.sections["local:t"].pill.text() == "running"
        assert window.sidebar.nav["devices"].count() == "2" and window.sidebar.nav["servers"].count() == "1"

        tile.clicked.emit()                                                  # what a click does
        page = window.device_page
        assert window.pages.currentWidget() is page and window._selected() == ("device", "local:t", "scope")
        assert page.header.title.text() == "scope" and page.header.pill.text() == "connected"
        assert "SimScope" in page.header.subtitle.text() and "Connected since" in page.instrument.text()
        assert spin(3, lambda: "Unix socket" in page.via.text())            # (in-process for a moment at start)
        assert spin(3, lambda: page.streams.rowCount() == 2)                 # ch1, ch2 ("state" has its own tab)
        assert page.streams.item(0, 0).text() == "ch1"
        assert page.tasks.item(0, 0).text() == "_acquire" and page.task_switch("_acquire").isChecked()
        assert page.state.rowCount() == 3                                    # timebase, signal_hz, live
        assert page.tabs.tabText(4) == "Commands (6)" and page.tabs.currentIndex() == 1  # it has no properties
        assert not page.connect_now.isEnabled() and page.release.isEnabled()
        window.go_back()
        assert window.pages.currentWidget() is window.devices_page and window._selected() is None

        window.open_server("local:t")
        spage = window.server_page
        assert spin(3, lambda: "ipc://" in spage.reach.text() and "tcp://" in spage.reach.text())
        assert "started by hand" in spage.service.text()
        assert spage.stop.isHidden()                                         # never offers to stop itself
        assert spage.latency.text().endswith("ms")
        assert set(spage.tiles) == {"scope", "loopback"} and not spage.no_clients.isHidden()
        spage.tiles["loopback"].clicked.emit()                               # a device of it, and back to it
        assert window._selected() == ("device", "local:t", "loopback") and page.header.back.text() == "t"
        window.go_back()
        assert window._selected() == ("server", "local:t")
        server.stop()
        assert spin(5, lambda: "local:t" not in window.servers)              # gone, and its page with it
        assert window.pages.currentWidget() is window.servers_page


def test_release_reopen_and_periodic_tasks(window):
    with insthub.Server("t", [sim.SimScope(), sim.Loopback()]) as server:
        assert spin(5, lambda: word(window, "device", "local:t", "loopback") == "connected")
        window.open_device("local:t", "loopback")
        window.device_page.release.click()
        assert spin(5, lambda: word(window, "device", "local:t", "loopback") == "released")
        assert server.devices["loopback"].connected is False
        assert "Released" in window.device_page.instrument.text() and window.device_page.connect_now.isEnabled()
        window.device_page.connect_now.click()
        assert spin(5, lambda: word(window, "device", "local:t", "loopback") == "connected")

        window.open_device("local:t", "scope")
        assert spin(3, lambda: window.device_page.tasks.rowCount() == 1)
        window.device_page.task_switch("_acquire").click()
        assert spin(5, lambda: not server.devices["scope"].tasks["_acquire"].enabled)
        assert spin(3, lambda: not window.device_page.task_switch("_acquire").isChecked())
        window.device_page.task_switch("_acquire").click()
        assert spin(5, lambda: server.devices["scope"].tasks["_acquire"].enabled)


class Lamp(insthub.Device):
    """A lamp with a brightness."""

    def open(self):
        self._on, self._level = False, 0.5

    @insthub.prop
    def on(self) -> bool:
        """Whether it is lit."""
        return self._on

    @on.setter
    def on(self, value):
        self._on = bool(value)

    @insthub.prop
    def level(self) -> float:
        """Brightness, 0 to 1."""
        return self._level

    @level.setter
    def level(self, value):
        if not 0 <= value <= 1:
            raise ValueError(f"level {value} outside 0-1")
        self._level = float(value)

    @insthub.prop
    def model(self) -> str:
        return "L-1"


def test_properties_are_read_and_set_in_place(window):
    with insthub.Server("t", [Lamp("lamp")]) as server:
        assert spin(5, lambda: word(window, "device", "local:t", "lamp") == "connected")
        window.open_device("local:t", "lamp")
        props = window.device_page.props
        assert window.device_page.tabs.currentIndex() == 0 and set(props.rows) == {"level", "model", "on"}
        level, model, on = props.rows["level"], props.rows["model"], props.rows["on"]
        assert spin(3, lambda: level.edit.text() == "0.5" and model.edit.text() == "L-1" and on.loaded)
        assert model.edit.isReadOnly() and model.set_button.isHidden()
        assert not on.switch.isHidden() and on.edit.isHidden() and not on.switch.isChecked()   # a bool: a switch

        level.edit.setText("0.8")
        level.apply()
        assert spin(3, lambda: server.devices["lamp"]._level == 0.8)
        assert spin(3, lambda: window.toast.text() == "lamp.level = 0.8")
        level.edit.setText("7")
        level.edit.returnPressed.emit()
        assert spin(3, lambda: "outside 0-1" in level.message.text())         # said where it was typed
        assert spin(3, lambda: level.edit.text() == "0.8") and "outside" in level.message.text()
        on.switch.click()
        assert spin(3, lambda: server.devices["lamp"]._on is True)


def test_commands_are_run_from_their_tab(window):
    with insthub.Server("t", [sim.Loopback()]):
        assert spin(5, lambda: word(window, "device", "local:t", "loopback") == "connected")
        window.open_device("local:t", "loopback")
        commands = window.device_page.commands
        assert not commands.run_button.isEnabled()
        commands.choose("echo")
        assert commands.run_button.isEnabled() and "echo" in commands.chosen.text()
        commands.args.setText('value=[1, "two"]')
        commands.run()
        assert spin(3, lambda: not commands.result.toPlainText().startswith("Running"))
        assert json.loads(commands.result.toPlainText()) == [1, "two"]
        assert commands.took.text().startswith("loopback.echo:")
        commands.choose("fail")
        commands.args.setText("RuntimeError 'no luck'")
        commands.args.returnPressed.emit()
        assert spin(3, lambda: commands.result.toPlainText() == "RuntimeError: no luck")
        commands.args.setText("'unclosed")
        commands.run()
        assert commands.result.toPlainText().startswith("Cannot read the arguments")


def test_previews_show_the_last_frame_without_subscribing(window, monkeypatch):
    window.show()                                                            # previews are fetched while shown
    with insthub.Server("t", [sim.SimScope(), sim.SimCamera(), sim.Loopback()], tcp=0) as server:
        scope = server.device("scope").subscribe("ch1")                     # somebody watches: frames flow
        camera = server.device("camera").subscribe("image")
        def tile(name):
            return window.devices_page.tiles.get(("local:t", name))

        def kind(name):
            tile = window.devices_page.tiles.get(("local:t", name))
            return tile.view.preview.kind if tile is not None and tile.view.preview is not None else None

        assert spin(8, lambda: kind("scope") == "trace" and kind("camera") == "image")
        trace = tile("scope").view.preview
        assert len(trace.data) == 1 and trace.data[0].shape == (320,) and trace.unit == "V"
        image = tile("camera").view.preview.data
        assert max(image.width(), image.height()) <= inventory.PREVIEW_PIXELS
        assert spin(3, lambda: tile("scope").view.caption.startswith("ch1"))
        assert server.devices["scope"].subscribers("ch1") == 1             # ours only: the window took none
        assert kind("loopback") is None and tile("loopback").view.message  # nothing published

        window.open_device("local:t", "scope")
        assert spin(3, lambda: not window.device_page.preview_box.isHidden())
        assert "float32[1000]" in window.device_page.preview_caption.text()
        window.show_page("devices")
        monkeypatch.setattr(pages, "STALE", 0.3)
        scope.close()
        assert spin(5, lambda: tile("scope").view.stale)        # no new frames: drawn faded
        assert "ago" in tile("scope").view.caption
        window.settings.setValue("previews", False)
        assert spin(3, lambda: kind("scope") is None and tile("scope").view.figures)
        camera.close()


def test_shrink_makes_frames_small():
    kind, data, unit = inventory.shrink(np.sin(np.linspace(0, 20, 100_000)), {"unit": "V"})
    assert kind == "trace" and data[0].shape == (2 * inventory.PREVIEW_POINTS,) and unit == "V"
    assert data[0].max() == pytest.approx(1, abs=1e-3) and data[0].min() == pytest.approx(-1, abs=1e-3)
    kind, data, _ = inventory.shrink(np.arange(2000 * 3000, dtype=np.uint16).reshape(2000, 3000), {})
    assert kind == "image" and data.dtype == np.uint8 and max(data.shape) <= inventory.PREVIEW_PIXELS
    assert inventory.shrink({"a": np.zeros(10), "b": np.ones(10), "t": 3}, {})[0] == "trace"
    assert inventory.shrink(np.float64(2.5), {"unit": "mW"}) == ("number", 2.5, "mW")
    assert inventory.shrink("hello", {}) == ("text", "hello", "")
    assert inventory.shrink(np.full(50, np.nan), {})[0] == "trace"                # no crash on NaN
    assert inventory.elements("uint16[480x640]") == 307200 and inventory.elements("float") == 0


def test_search_filters_the_tiles(window):
    with insthub.Server("t", [sim.SimScope(), sim.Loopback()]):
        page = window.devices_page
        assert spin(5, lambda: word(window, "device", "local:t", "scope") == word(window, "device", "local:t",
                                                                                   "loopback") == "connected")
        page.search.setText("LOOP")
        assert page.tiles["local:t", "scope"].isHidden() and not page.tiles["local:t", "loopback"].isHidden()
        page.search.setText("simscope")                                      # its class counts too
        assert not page.tiles["local:t", "scope"].isHidden() and page.tiles["local:t", "loopback"].isHidden()
        page.search.setText("nothing like it")
        assert page.sections["local:t"].isHidden() and "No device matches" in page.nothing.text()
        page.search.clear()
        assert not page.sections["local:t"].isHidden() and page.nothing.isHidden()


def test_other_programs_and_stopping_a_server(window, spawned, monkeypatch):
    spawned.start()
    watcher = subprocess.Popen([sys.executable, "-m", "insthub", "watch", "scope", "ch1"], env=os.environ.copy(),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert spin(10, lambda: word(window, "device", "local:child", "scope") == "connected")
        window.open_device("local:child", "scope")
        if sys.platform.startswith("linux"):                                 # programs are named from /proc
            assert spin(10, lambda: "insthub watch scope ch1 (ch1)" in window.device_page.watchers.text())
        window.open_server("local:child")
        assert spin(3, lambda: window.server_page.clients.rowCount() == 1)
        assert window.server_page.clients.item(0, 3).text() == "scope/ch1"
        assert not window.server_page.stop.isHidden()
        asked = []
        monkeypatch.setattr(window, "confirm", lambda *a: asked.append(a) or True)
        window.server_page.stop.click()
        spawned.proc.wait(10)
        assert asked and spawned.proc.returncode == 0                        # SIGTERM: a clean shutdown
        assert spin(5, lambda: "local:child" not in window.servers)
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
            awg = pages.device_view(window.servers["remote:tcp://127.0.0.1:1"], "awg")
            assert awg.message.startswith("Its server does not answer")
            assert window.servers["local:t"].service["name"] == "t"          # matched by pid
            assert window.add_service.isEnabled()
            lab = window.devices_page.sections["service:lab"]
            assert spin(3, lambda: not lab.start.isHidden() and "Logs shows why" in lab.note.text())
            assert window.sidebar.nav["servers"].count() == "3"

            window.open_server("local:t")
            page = window.server_page
            assert "insthub-t · active · starts at login" in page.service.text()
            assert page.start.isHidden() and not page.restart.isHidden()
            page.restart.click()
            page.stop.click()                                                 # a service is stopped by systemd
            window.open_server("service:lab")
            assert "Logs shows why" in page.state.text() and not page.start.isHidden()
            page.start.click()
            assert spin(5, lambda: len(calls) == 3)
            assert calls == [("restart", "t"), ("stop", "t"), ("start", "lab")]

            window.open_server("remote:tcp://127.0.0.1:1")
            assert "Does not answer" in page.state.text() and "devices.toml" in page.header.subtitle.text()
            assert page.forget.isVisible() or not page.more.isHidden()
            window.forget_remote(window.servers["remote:tcp://127.0.0.1:1"])  # Remove from devices.toml
            assert spin(5, lambda: "remote:tcp://127.0.0.1:1" not in window.servers)
            assert registry.static_devices() == {}
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


def test_tray_keeps_watching_and_tells_when_something_breaks(monkeypatch, tmp_path):
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
            assert window.sidebar.nav["devices"]._alert and "1 not connected" in window.summary.text()
            assert pages.device_view(window.servers["local:t"], "flaky").message == "Not connected: OSError: cable pulled"

            login = next(a for a in window.tray.menu.actions() if a.text().startswith("Start at login"))
            login.trigger()
            assert '"gui" "--tray"' in launcher.autostart_path().read_text() and launcher.autostart()
            login.trigger()
            assert not launcher.autostart()
            notify = next(a for a in window.tray.menu.actions() if a.text().startswith("Notify me"))
            notify.trigger()
            assert window.tray.notify is False
            window.show_page("settings")
            assert not window.settings_page.notify.isChecked()           # the same setting as the tray's

            window.quit()
            assert window._closed and not window.tray.isVisible()
        finally:
            window._shutdown()
            spin(0.1)


def test_tray_icon_is_a_status_light():
    qt.application()

    def pixel(status, x, y):
        return qt.QtGui.QColor(manager._tray_icon(status).pixmap(32, 32).toImage().pixel(x, y))

    assert pixel(None, 16, 16).lightness() > 200                         # the hub: white, like the top bar's icons
    assert pixel("good", 25, 25).name() == "#33d17a" and pixel("bad", 25, 25).name() == "#ed333b"
    assert pixel(None, 25, 25).lightness() > 200                         # no light: an ordinary node


class Agent:
    """`insthub mcp` started the way an agent starts it, asked to list the devices."""

    def __init__(self):
        self.proc = subprocess.Popen([sys.executable, "-m", "insthub", "mcp"], env=os.environ.copy(), text=True,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def ask(self, mid, method, params=None):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
                              + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def list_devices(self):
        self.ask(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "0"}})
        return self.ask(2, "tools/call", {"name": "list_devices", "arguments": {}})

    def stop(self):
        self.proc.stdin.close()
        self.proc.wait(10)
        self.proc.stdout.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="programs are named from /proc")
def test_ai_agents_are_shown_and_announced(monkeypatch):
    assert inventory.agent_name(["/home/u/.local/bin/claude", "--output-format", "stream-json"]) == "Claude Code"
    assert inventory.agent_name(["/usr/bin/python3", "my_agent.py"]) == "my_agent.py"
    assert inventory.agent_name([]) == "unknown"
    qt.application()
    with insthub.Server("t", [sim.Loopback()]):
        window = manager.Manager(refresh_ms=150, tray=True)
        told = []
        monkeypatch.setattr(window.tray, "showMessage", lambda title, text, *a: told.append((title, text)))
        agent = Agent()
        try:
            assert spin(5, lambda: word(window, "device", "local:t", "loopback") == "connected")
            assert not window.agents()
            assert "loopback" in agent.list_devices()["result"]["content"][0]["text"]
            assert spin(5, lambda: window.agents())
            (pid, (name, servers)), = window.agents().items()
            assert pid == agent.proc.pid and name.startswith("AI agent (pytest") and servers == ["t"]
            assert inventory.program_name(agent.proc.pid) == name
            assert "1 AI agent connected" in window.summary.text() and window.sidebar.nav["agents"].count() == "1"
            assert spin(3, lambda: told)
            assert told == [("An AI agent is using the instruments", f"{name} connected to t.")]
            assert f"{name} on t" in window.tray.toolTip()
            assert f"{name} on t" in [a.text() for a in window.tray.menu.actions()]
            window.open_server("local:t")
            assert spin(3, lambda: window.server_page.clients.rowCount() == 1)
            assert window.server_page.clients.item(0, 0).text() == name
            agent.stop()
            assert spin(5, lambda: not window.agents())                  # gone when it leaves
        finally:
            if agent.proc.poll() is None:
                agent.stop()
            window._shutdown()
            spin(0.1)


def test_agents_page_sets_up_claude_code(window, monkeypatch):
    real_which = shutil.which
    installed = {"yes": True}
    monkeypatch.setattr(pages.shutil, "which",
                        lambda name: ("/usr/bin/claude" if installed["yes"] else None) if name == "claude"
                        else real_which(name))
    registered, calls = set(), []

    def claude(*args):
        calls.append(args)
        if args[:2] == ("mcp", "get"):
            code = 0 if args[2] in registered else 1
        elif args[:2] == ("mcp", "add"):
            registered.add(args[4])
            code = 0
        else:
            registered.discard(args[4])
            code = 0
        return subprocess.CompletedProcess(("claude", *args), code, "", "")

    monkeypatch.setattr(pages, "_claude", claude)
    window.show_agents()
    page = window.agents_page
    assert window.pages.currentWidget() is page
    assert spin(3, lambda: page.status.text() == "not set up yet")
    assert not page.setup.isHidden() and page.remove.isHidden()
    page.setup.click()
    assert spin(3, lambda: "set up, for all your projects" in page.status.text())
    add = next(c for c in calls if c[:2] == ("mcp", "add"))
    assert add[:6] == ("mcp", "add", "--scope", "user", "insthub", "--") and add[-1] == "mcp"
    assert add[6:] == tuple(inventory.mcp_command())
    page.remove.click()
    assert spin(3, lambda: page.status.text() == "not set up yet")
    assert "No agent is connected" in page.connected.text()
    installed["yes"] = False
    page.check()
    assert "Not installed here" in page.status.text() and page.setup.isHidden()


def test_new_server_writes_its_config_and_starts_it(window, monkeypatch, tmp_path):
    monkeypatch.setattr(forms, "servers_folder", lambda: tmp_path / "servers")
    started = []
    monkeypatch.setattr(window, "start_server", lambda command, name, cwd=None: started.append((command, name)))
    dialog = forms.NewServerDialog(window)
    assert dialog.dev_name.text() == "scope" and set(dialog._arg_edits) == {"points", "latency"}
    assert dialog._arg_edits["points"].placeholderText() == "1000"           # the driver's default
    dialog.name.setText("bench")
    dialog._arg_edits["points"].setText("500")
    dialog._arg_edits["points"].textEdited.emit("500")
    dialog._add()
    dialog.driver.setCurrentText("insthub.sim:Loopback")
    assert dialog.dev_name.text() == "loopback"                              # named after the driver
    dialog.reach.setCurrentIndex(1)
    dialog.port.setValue(8799)
    assert '[devices.loopback]\ndriver = "insthub.sim:Loopback"' in dialog.preview.toPlainText()
    dialog.dev_name.setText("scope")                                         # two of the same name: refused
    dialog.dev_name.textEdited.emit("scope")
    assert "same name" in dialog.problem.text() and not dialog.create.isEnabled()
    dialog.dev_name.setText("echo")
    dialog.dev_name.textEdited.emit("echo")
    assert dialog.create.isEnabled()
    dialog.then.setCurrentIndex(dialog.then.findData(dialog.START_ONCE))
    dialog.create.click()
    path = tmp_path / "servers" / "bench.toml"
    kwargs, devices = config.load(path)
    assert kwargs == {"name": "bench", "tcp": "0.0.0.0:8799"} and [d.name for d in devices] == ["scope", "echo"]
    assert devices[0].points == 500
    assert started == [([sys.executable, "-m", "insthub", "serve", str(path)], "bench")]


def test_drivers_are_read_without_running_them(tmp_path):
    (tmp_path / "psu.py").write_text(
        "raise SystemExit('imported!')\n"                                    # reading it must not run it
        "class PowerSupply:\n"
        "    def __init__(self, port, baud=9600, *, name='x', timeout=1.0, mode='CV'):\n"
        "        pass\n"
        "class _Helper:\n    pass\n"
        "class Plain:\n    pass\n")
    assert forms.classes_in((tmp_path / "psu.py").read_text()) == {
        "PowerSupply": [("port", None, False), ("baud", "9600", False), ("timeout", "1.0", False),
                        ("mode", "CV", True)], "Plain": []}
    assert forms.constructor_args("psu:PowerSupply", str(tmp_path))[0] == ("port", None, False)
    assert forms.constructor_args("psu:PowerSupply", "") is None             # where is it? unknown
    assert forms.constructor_args("insthub.sim:SimScope") == [("points", "1000", False), ("latency", "0.0", False)]
    spec = forms.DeviceSpec("psu", "psu:PowerSupply", str(tmp_path), args={"port": "COM3", "baud": "19200",
                                                                          "mode": "1"}, texts={"mode"},
                            extra="# a comment\nretries = 3")
    assert spec.table() == {"driver": "psu:PowerSupply", "path": str(tmp_path), "port": "COM3", "baud": 19200,
                            "mode": "1", "retries": 3}
    with pytest.raises(ValueError, match="key = value"):
        forms.DeviceSpec("psu", "psu:PowerSupply", extra="retries").table()
    with pytest.raises(ValueError, match="module:Class"):
        forms.DeviceSpec("psu", "PowerSupply").table()


def test_devices_on_another_computer_are_added_to_devices_toml(window):
    with insthub.Server("far", [sim.Loopback("echo")], tcp=0) as server:
        address = server.addresses[1]
        dialog = forms.RemoteDialog(window)
        dialog.where.setText("nowhere")
        dialog.ask()
        assert "host:port" in dialog.status.text()
        dialog.where.setText(address.removeprefix("tcp://"))
        dialog.ask()
        assert spin(5, lambda: dialog.list.count() == 1)
        assert "“far”" in dialog.status.text() and dialog.add.text() == "Add 1 device"
        dialog.add.click()
        assert registry.static_devices() == {"echo": address}
        assert "echo added to" in window.toast.text()


def test_theme_follows_the_setting(window):
    window.show_page("settings")
    page = window.settings_page
    page.theme.setCurrentIndex(page.theme.findData("dark"))
    assert ui.current() is ui.DARK and ui.DARK.window in window.styleSheet()
    assert window.settings.value("theme") == "dark"
    page.theme.setCurrentIndex(page.theme.findData("light"))
    assert ui.current() is ui.LIGHT and ui.LIGHT.window in window.styleSheet()


def test_every_page_paints_in_both_themes(window, tmp_path):
    window.resize(1100, 720)
    window.show()
    with insthub.Server("t", [*sim.devices(), Lamp("lamp")], tcp=0) as server:
        server.device("synth").rf(True)
        sub = server.device("sa").subscribe("trace")
        assert spin(8, lambda: word(window, "device", "local:t", "sa") == "connected"
                    and window.servers["local:t"].previews.get("sa") is not None)
        for theme in ("light", "dark"):
            window.settings.setValue("theme", theme)
            window.apply_theme()
            for show in (lambda: window.show_page("devices"), lambda: window.open_device("local:t", "sa"),
                         lambda: window.open_device("local:t", "lamp"), lambda: window.show_page("servers"),
                         lambda: window.open_server("local:t"), lambda: window.show_page("agents"),
                         lambda: window.show_page("settings")):
                show()
                spin(0.2)
                assert not window.grab().isNull()
        for dialog in (forms.NewServerDialog(window), forms.ServiceDialog(window), forms.RemoteDialog(window)):
            dialog.show()
            spin(0.1)
            assert not dialog.grab().isNull()
            dialog.close()
        sub.close()


def test_grid_and_figures():
    qt.application()
    host = qt.QtWidgets.QWidget()
    grid = ui.GridFlow(host, min_width=250, gap=14)
    tiles = [ui.Tile(parent=host) for _ in range(5)]
    for tile in tiles:
        grid.addWidget(tile)
    assert grid.columns(1000) == 3 and grid.columns(200) == 1
    assert grid.heightForWidth(1000) == 2 * ui.Tile.HEIGHT + 14             # 3 + 2
    tiles[0].hide()
    assert grid.heightForWidth(800) == 2 * ui.Tile.HEIGHT + 14              # 4 left, 3 to a row at 800
    grid.set_order(list(reversed(tiles)))
    assert grid.itemAt(0).widget() is tiles[-1]
    assert [ui.format_value(v) for v in (True, 3, 1000.0, 0.001, 2.5e9, 1e-7, [480, 640], "x")] == \
        ["on", "3", "1000", "0.001", "2.5e9", "1e-7", "480 × 640", "x"]


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
    name = inventory.command_name
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
    assert inventory.find_python(tmp_path / "rig" / "servers" / "lab.toml") == str(python)   # the folder above
    assert inventory.find_python(tmp_path / "other.toml") == sys.executable


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

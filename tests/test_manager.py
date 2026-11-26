"""The manager window (`insthub gui`) and its launcher, run without a display."""
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import time
import tomllib

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("insthub.qt", exc_type=ImportError)  # needs a Qt binding

import insthub  # noqa: E402
from insthub import (  # noqa: E402
    cli,
    config,
    extensions,
    forms,
    inventory,
    launcher,
    manager,
    pages,
    qt,
    registry,
    service,
    sim,
)
from insthub import widgets as ui  # noqa: E402


def spin(seconds, until=lambda: False):
    app = qt.application()
    end = time.monotonic() + seconds
    while time.monotonic() < end and not until():
        app.processEvents()
        time.sleep(0.01)
    return until()


@pytest.fixture(autouse=True)
def no_real_services(monkeypatch):
    """Not the services installed on this computer: a test that needs some gives its own."""
    monkeypatch.setattr(service, "units", list)


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


class Rig(insthub.Device):
    """A rig whose settings say what they take."""

    simulated = True
    extension, extension_version = "rig-ext", "0.4"

    def open(self):
        self.values, self.fired = {"mode": "CW", "level": 2.5, "gain": "auto"}, 0

    def _get(name):
        return lambda self: self.values[name]

    def _set(name):
        return lambda self, value: self.values.__setitem__(name, value)

    mode = insthub.prop(_get("mode"), _set("mode"), choices=["CW", "pulsed"])
    level = insthub.prop(_get("level"), _set("level"), unit="V", min=0, max=10, step=0.5, dangerous=True)
    gain = insthub.prop(_get("gain"), _set("gain"), unit="dB", min=0, max=40)  # it says "auto" for now

    @insthub.command(dangerous=True, params={"ms": {"unit": "ms", "min": 0}})
    def fire(self, ms=10):
        self.fired += 1
        return ms


def test_controls_follow_what_a_device_says_and_dangerous_asks_first(window, monkeypatch):
    with insthub.Server("t", [Rig("rig")]) as server:
        rig = server.devices["rig"]
        assert spin(5, lambda: word(window, "device", "local:t", "rig") == "connected")
        assert spin(3, lambda: ("local:t", "rig") in window.devices_page.tiles)
        tile = window.devices_page.tiles["local:t", "rig"]
        assert tile.view.badge == "SIM" and "not a measurement" in tile.view.tip
        window.open_device("local:t", "rig")
        page = window.device_page
        assert not page.header.badge.isHidden() and page.extension.text() == "rig-ext 0.4"
        assert spin(3, lambda: "extension rig-ext is not installed on this computer: insthub ext add FOLDER"
                    in page.extension.text())                                # its extension: not here
        rows = page.props.rows
        assert spin(3, lambda: all(row.loaded for row in rows.values()))
        mode, level, gain = rows["mode"], rows["level"], rows["gain"]
        assert mode.editor is mode.choice and mode.choice.currentText() == "CW"
        assert level.editor is level.number and level.number.text() == "2.5 V" and level.number.maximum() == 10
        assert gain.editor is gain.edit and gain.edit.text() == "auto"     # what a spin box cannot show
        mode.choice.activated.emit(1)                                        # a choice is sent at once
        assert spin(3, lambda: rig.values["mode"] == "pulsed")

        answers = []
        monkeypatch.setattr(window, "confirm", lambda *a: answers.append(a[0]) or a[0].startswith("Set"))
        level.number.setValue(4)
        level.apply()
        assert answers == ["Set rig.level?"] and spin(3, lambda: level.value == 4.0)   # set, and read again
        monkeypatch.setattr(window, "confirm", lambda *a: answers.append(a[0]) and False)
        level.number.setValue(6)
        level.number.submitted.emit()                                        # Enter
        assert answers[-1] == "Set rig.level?" and level.number.value() == 4  # declined: shows what it has
        spin(0.2)
        assert rig.values["level"] == 4.0

        commands = page.commands
        commands.choose("fire")
        assert "ms: at least 0 ms" in commands.chosen.text() and "asked before it runs" in commands.chosen.text()
        assert commands.table.item(0, 2).text().startswith("Dangerous")
        commands.run()                                                       # declined
        assert answers[-1] == "Run rig.fire?" and commands.result.toPlainText() == ""
        monkeypatch.setattr(window, "confirm", lambda *a: True)
        commands.args.setText("ms=5")
        commands.run()
        assert spin(3, lambda: commands.result.toPlainText() == "5") and rig.fired == 1


class Shutter:
    """A driver whose output is an accessor pair: get_output() and set_output(on)."""

    def __init__(self):
        self.on = False

    def get_output(self):
        return self.on

    def set_output(self, on):
        self.on = on


def test_writing_a_property_whose_setter_is_dangerous_asks_first(window, monkeypatch):
    shutter = insthub.wrap(Shutter, "shutter", accessors=True, dangerous=["set_output"])  # the command is marked
    with insthub.Server("t", [shutter]):
        assert spin(5, lambda: word(window, "device", "local:t", "shutter") == "connected")
        window.open_device("local:t", "shutter")
        rows = window.device_page.props.rows
        assert spin(3, lambda: "output" in rows and rows["output"].loaded)
        asked = []
        monkeypatch.setattr(window, "confirm", lambda *a: asked.append(a[0]) and False)
        window.device_page.props.write("output", True)  # what a property's editor does
        spin(0.2)
        assert asked == ["Set shutter.output?"] and shutter.target.on is False


def test_number_box_shows_numbers_as_typed():
    qt.application()
    box = ui.NumberBox()
    box.set_meta({"unit": "MHz", "min": 10, "max": 24000})
    box.setValue(1000.25)
    assert box.text() == "1000.25 MHz" and box.fits(24000) and not box.fits(9)
    assert not ui.NumberBox().fits(1e-12) and ui.NumberBox().fits(-0.001)  # too small for its decimals: no


def test_commands_are_run_from_their_tab(window):
    with insthub.Server("t", [sim.Loopback()]):
        assert spin(5, lambda: word(window, "device", "local:t", "loopback") == "connected")
        window.open_device("local:t", "loopback")
        commands = window.device_page.commands
        assert not commands.run_button.isEnabled()
        assert commands.styleSheet() == window.device_page.props.styleSheet() == ""  # the window's look, not their own
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
        commands.choose("sleep")
        commands.args.setText("0.3")
        commands.run()
        s, page = window.servers["local:t"], window.device_page
        probe, s.probe = s.probe, None                                       # one probe fails while it runs ...
        page.show_device(s, "loopback")
        s.probe = probe
        page.show_device(s, "loopback")
        assert commands.current() == "sleep" and commands.args.text() == "0.3"  # ... it is the same device
        assert spin(3, lambda: commands.took.text().startswith("loopback.sleep:"))  # and its result is shown


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
    (tmp_path / "devices.toml").write_text('[devices]\nawg = "tcp://127.0.0.1:1"\n\n[servers]\nrig = "tcp://127.0.0.1:2"\n')
    monkeypatch.setenv("INSTHUB_DEVICES", str(tmp_path / "devices.toml"))
    unit = {"enabled": True, "command": '"python" "-m" "insthub" "serve" "t.toml"', "unit": "u"}
    monkeypatch.setattr(service, "units", lambda: [
        {**unit, "name": "t", "state": "active", "sub": "running", "pid": os.getpid(), "restarts": 2},
        {**unit, "name": "lab", "state": "failed", "sub": "failed", "pid": None, "restarts": 5,
         "result": "start-limit-hit"}])
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
            assert spin(5, lambda: word(window, "server", "remote:tcp://127.0.0.1:2") == "unreachable")  # [servers]
            awg = pages.device_view(window.servers["remote:tcp://127.0.0.1:1"], "awg")
            assert awg.message.startswith("Its server does not answer")
            assert window.servers["local:t"].service["name"] == "t"          # matched by pid
            assert window.add_service.isEnabled()
            lab = window.devices_page.sections["service:lab"]
            assert spin(3, lambda: not lab.start.isHidden() and "Logs shows why" in lab.note.text())
            assert "(restarted 5 times, gave up: too many restarts)" in lab.note.text()
            assert window.sidebar.nav["servers"].count() == "4"

            window.open_server("local:t")
            page = window.server_page
            assert "insthub-t · active · restarted 2 times" in page.service.text() and page.login.isChecked()
            assert page.start.isHidden() and not page.restart.isHidden()
            page.restart.click()
            page.stop.click()                                                 # a service is stopped by systemd
            window.open_server("service:lab")
            assert "Logs shows why" in page.state.text() and not page.start.isHidden()
            page.start.click()
            assert spin(5, lambda: len(calls) == 3)
            assert calls == [("restart", "t"), ("stop", "t"), ("start", "lab")]

            window.open_server("remote:tcp://127.0.0.1:2")
            assert not page.forget.isVisible()                               # [servers]: nothing it may remove
            window.open_server("remote:tcp://127.0.0.1:1")
            assert "Does not answer" in page.state.text() and "devices.toml" in page.header.subtitle.text()
            assert page.forget.isVisible() or not page.more.isHidden()
            window.forget_remote(window.servers["remote:tcp://127.0.0.1:1"])  # Remove from devices.toml
            assert spin(5, lambda: "remote:tcp://127.0.0.1:1" not in window.servers)
            assert registry.static_devices() == {}
        finally:
            window.close()
            spin(0.1)


def test_a_server_here_that_devices_toml_names_by_another_address_has_one_card(window):
    with insthub.Server("lan", [sim.Loopback()], tcp=0) as srv:
        key = f"remote:tcp://localhost:{srv.addresses[-1].rpartition(':')[2]}"   # an address it does not announce
        registry.devices_file().write_text(f'[servers]\nlabpc = "{key[7:]}"\n')
        assert spin(5, lambda: word(window, "device", "local:lan", "loopback") == "connected"
                    and key[7:] in window._here and key not in window.servers)       # it was asked, and is ours
        assert not spin(1, lambda: key in window.servers)                            # not shown again by a scan
        assert [k for k in window.servers if k.startswith("remote:")] == []


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
    _choose(dialog, "loopback")                                              # one insthub's own extension offers
    assert dialog.dev_name.text() == "loopback"                              # named after it
    dialog.reach.setCurrentIndex(dialog.reach.findData(forms.NETWORK))
    dialog.port.setValue(8799)
    assert '[devices.loopback]\ndriver = "insthub.sim:Loopback"\nextension = "insthub-sim"' in \
        dialog.preview.toPlainText()
    dialog.dev_name.setText("scope")                                         # two of the same name: refused
    dialog.dev_name.textEdited.emit("scope")
    assert "same name" in dialog.problem.text() and not dialog.create.isEnabled()
    dialog.dev_name.setText("echo")
    dialog.dev_name.textEdited.emit("echo")
    assert dialog.create.isEnabled()
    dialog.then.setCurrentIndex(dialog.then.findData(dialog.ONCE))
    dialog.create.click()
    path = tmp_path / "servers" / "bench.toml"
    kwargs, devices = config.load(path)
    assert kwargs == {"name": "bench", "tcp": "0.0.0.0:8799"} and [d.name for d in devices] == ["scope", "echo"]
    assert devices[0].points == 500
    assert started == [([sys.executable, "-m", "insthub", "serve", str(path)], "bench")]


def test_new_server_offers_the_devices_of_extensions(window, monkeypatch, tmp_path):
    folder = tmp_path / "hp"
    (folder / ".venv" / "bin").mkdir(parents=True)
    (folder / ".venv" / "bin" / "python").write_text("")
    (folder / "hpdrv.py").write_text("class Analyzer:\n    def __init__(self, pad=1, sim=False):\n        pass\n")
    (folder / "insthub-extension.toml").write_text(
        '[extension]\nid = "hp8562a"\ntitle = "HP 8562A"\n\n[devices.analyzer-sim]\ndriver = "hpdrv:Analyzer"\n'
        'doc = "a simulated analyzer"\nsim = true\nexclude = ["write"]\n')
    monkeypatch.setenv("INSTHUB_EXTENSIONS", str(folder))
    dialog = forms.NewServerDialog(window)
    shown = lambda: [dialog.offers.item(r).text() for r in range(dialog.offers.rowCount())]
    assert shown()[0] == "Simulated instruments: a simulated two-channel oscilloscope"  # insthub's own, at once
    assert spin(3, lambda: "HP 8562A: a simulated analyzer" in shown())  # the others once found
    assert dialog.driver.currentText() == "insthub.sim:SimScope"  # what the field held stays
    dialog._add()
    _choose(dialog, "analyzer-sim")
    assert dialog.dev_name.text() == "analyzer-sim" and dialog.driver.currentText() == "hpdrv:Analyzer"
    assert dialog.dev_folder.text() == str(folder) and dialog.python.text() == str(folder / ".venv/bin/python")
    assert dialog._arg_edits["sim"].isReadOnly() and not dialog._arg_edits["pad"].isReadOnly()  # given: as is
    table = tomllib.loads(dialog.preview.toPlainText())["devices"]["analyzer-sim"]
    assert table == {"driver": "hpdrv:Analyzer", "path": str(folder), "extension": "hp8562a", "sim": True,
                     "exclude": ["write"]}


def _extension(folder, ext_id, offers, python=".venv/bin/python"):
    """An extension's folder: a Python of its own (a stand-in), a driver module, and a manifest that offers
    each of `offers` (name -> more lines of its table) with that one driver."""
    (folder / python).parent.mkdir(parents=True)
    (folder / python).write_text("")
    (folder / f"drv_{ext_id}.py").write_text("class D:\n    def __init__(self, pad=1, sim=False):\n        pass\n")
    declared = "" if python == ".venv/bin/python" else f'python = "{python}"\n'
    (folder / "insthub-extension.toml").write_text(f'[extension]\nid = "{ext_id}"\ntitle = "{ext_id.upper()}"\n'
                                                   f'{declared}' + "".join(
        f'\n[devices.{name}]\ndriver = "drv_{ext_id}:D"\ndoc = "{name} of {ext_id}"\n{more}' for name, more in offers.items()))
    return folder


def test_a_device_chosen_from_the_list_is_named_after_it_until_it_is_another(window, monkeypatch, tmp_path):
    hp = _extension(tmp_path / "hp", "hp", {"spectrum": "pad = 18\n", "spectrum-sim": "sim = true\n"})  # one driver
    monkeypatch.setenv("INSTHUB_EXTENSIONS", str(hp))
    dialog = forms.ServerDialog(window)
    assert spin(3, lambda: any(dialog.offers.item(r).data(forms._OFFER)[0].id == "hp"
                               for r in range(dialog.offers.rowCount())))
    devices = lambda: tomllib.loads(dialog.preview.toPlainText())["devices"]
    _choose(dialog, "spectrum")                                       # the first device, named by the dialog too
    assert dialog.dev_name.text() == "spectrum"
    _choose(dialog, "scope")
    assert dialog.dev_name.text() == "scope" and devices()["scope"] == {"driver": "insthub.sim:SimScope",
                                                                         "extension": "insthub-sim"}
    dialog._add()
    _choose(dialog, "spectrum-sim")                                   # the field gets the driver first: two offer it
    assert dialog.dev_name.text() == "spectrum-sim"
    assert devices()["spectrum-sim"] == {"driver": "drv_hp:D", "path": str(hp), "extension": "hp", "sim": True}
    _choose(dialog, "spectrum")                                       # the name was the dialog's: chosen again
    assert devices()["spectrum"] == {"driver": "drv_hp:D", "path": str(hp), "extension": "hp", "pad": 18}
    _type(dialog.dev_name, "rf")                                      # a name of its own stays
    _choose(dialog, "spectrum-sim")
    assert dialog.dev_name.text() == "rf"
    dialog.extra.setPlainText(dialog.extra.toPlainText() + "\nbaud = 9600")
    dialog.driver.setEditText("mydrv:Other")                          # another driver: no longer the extension's
    assert devices()["rf"] == {"driver": "mydrv:Other", "path": str(hp), "baud": 9600}  # what you wrote stays


def test_a_new_server_runs_in_its_devices_python_and_says_what_does_not_fit(window, monkeypatch, tmp_path):
    a, b = _extension(tmp_path / "a", "aaa", {"aaadev": ""}), _extension(tmp_path / "b", "bbb", {"bbbdev": ""})
    py_a, py_b = str(a / ".venv/bin/python"), str(b / ".venv/bin/python")
    monkeypatch.setenv("INSTHUB_EXTENSIONS", os.pathsep.join([str(a), str(b)]))
    dialog = forms.ServerDialog(window)
    assert spin(3, lambda: dialog.offers.offer("drv_bbb:D") is not None)
    dialog._add()
    _choose(dialog, "aaadev")
    assert dialog.python.text() == py_a and dialog.driver_note.isHidden()
    dialog._add()
    _choose(dialog, "bbbdev")
    assert dialog.python.text() == py_a                               # a server runs in one Python ...
    note = dialog.driver_note.text()                                  # ... and the device it does not suit says so
    assert not dialog.driver_note.isHidden() and "Its extension, BBB, runs in " + py_b in note
    assert f"the server runs in {py_a}, which aaadev needs" in note and "separate servers" in note
    dialog.list.setCurrentRow(1)
    assert dialog.driver_note.isHidden()                              # aaadev: in its Python
    dialog._remove()
    assert dialog.python.text() == py_b and dialog.driver_note.isHidden()  # bbbdev's, now that it alone needs one
    _type(dialog.python, "/opt/py/bin/python")                        # chosen by hand: it stays
    dialog._add()
    _choose(dialog, "aaadev")
    assert dialog.python.text() == "/opt/py/bin/python"
    assert "but the server is to run in /opt/py/bin/python" in dialog.driver_note.text()


def test_adding_an_extension_s_device_says_when_its_server_runs_another_python(window, monkeypatch, tmp_path):
    a = _extension(tmp_path / "a", "aaa", {"aaadev": ""}, python="py/python3")  # named in its manifest
    monkeypatch.setenv("INSTHUB_EXTENSIONS", str(a))
    path = tmp_path / "rig.toml"
    path.write_text('[devices.echo]\ndriver = "insthub.sim:Loopback"\n')
    monkeypatch.setattr(service, "units", lambda: [{"name": "rig", "state": "inactive", "sub": "dead", "pid": None,
                                                    "enabled": False, "unit": "u",
                                                    "command": f'"/srv/py/bin/python" "-m" "insthub" "serve" "{path}"'}])
    assert spin(5, lambda: "service:rig" in window.servers)
    dialog = forms.ServerDialog(window, path, server=window.servers["service:rig"], add=True)
    assert spin(3, lambda: dialog.offers.offer("drv_aaa:D") is not None)
    _choose(dialog, "aaadev")
    note = dialog.driver_note.text()
    assert f"Its extension, AAA, runs in {a / 'py' / 'python3'}, but rig runs in /srv/py/bin/python" in note


def menu_of(window, monkeypatch, key, name) -> list[str]:
    """What a right-click on a device's tile offers."""
    shown = []
    monkeypatch.setattr(qt.QtWidgets.QMenu, "exec", lambda self, *a: shown.extend(a.text() for a in self.actions()))
    window.device_menu(key, name, None)
    return shown


def test_a_device_opens_the_uis_of_its_extension(window, monkeypatch):
    """Open runs a UI of the device's extension as a program of its own, with the extension's Python
    (insthub's own panel for its scope: this Python); the window imports none of it."""
    monkeypatch.delitem(sys.modules, "insthub.sim_ui", raising=False)   # (an earlier test may have imported it)
    monkeypatch.delattr(insthub, "sim_ui", raising=False)
    with insthub.Server("t", [sim.SimScope(), sim.SimSynth()]) as server:
        assert spin(5, lambda: word(window, "device", "local:t", "scope") == "connected")
        assert spin(5, lambda: "Open Scope panel" in menu_of(window, monkeypatch, "local:t", "scope"))
        assert not any(text.startswith("Open ") for text in menu_of(window, monkeypatch, "local:t", "synth"))
        window.open_device("local:t", "synth")
        page = window.device_page
        assert spin(5, lambda: window.device_uis(window.servers["local:t"], "synth") == (
            extensions.load(extensions.OWN, "insthub"), [], None))         # found out: the scope's UI only
        assert page.ui_buttons.count() == 0
        window.open_device("local:t", "scope")
        assert spin(5, lambda: page.ui_buttons.count() == 1)
        button = page.ui_buttons.itemAt(0).widget()
        assert button.text() == "Open Scope panel" and button.isEnabled() and sys.executable in button.toolTip()
        button.click()
        panel, = (p for p in window._spawned if p.title == "Scope panel for scope")
        try:                                                             # it watches the scope, from its program
            assert spin(20, lambda: server.devices["scope"].subscribers("ch1") > 0), panel.log.read_text()
            assert panel.process.args[1:4] == ["-m", "insthub", "-a"] and "panel" in panel.process.args
        finally:
            panel.process.terminate()
            panel.process.wait(10)
    assert "insthub.sim_ui" not in sys.modules


def test_a_window_opened_for_a_device_says_why_it_failed(window, monkeypatch, tmp_path):
    said = []
    monkeypatch.setattr(window, "say", lambda text, ms=5000: said.append(text))
    gone = inventory.Server("local:gone", "gone", "local", ["tcp://127.0.0.1:9"])  # nothing answers there
    window.live_view(gone, "scope")                                      # its output goes to a log, which says why
    assert spin(20, lambda: any(t.startswith("The live view of scope ended with an error: ") for t in said)), said
    assert inventory.ui_log("scope").read_text().strip() and str(inventory.ui_log("scope")) in said[-1]
    folder = tmp_path / "hp"
    folder.mkdir()
    (folder / "insthub-extension.toml").write_text(
        f'[extension]\nid = "hp"\npython = "{sys.executable}"\n\n[ui.quick]\ntitle = "Quick look"\n'
        'run = ["-c", "import sys; sys.exit(\'No module named PyQt6: install the gui extra\')"]\n')
    ext = extensions.load(folder / "insthub-extension.toml")
    window.open_ui(gone, "sa", ext, ext.ui["quick"])
    assert spin(20, lambda: "Quick look for sa ended with an error: No module named PyQt6: install the gui extra "
                f"(its log: {inventory.ui_log('sa', 'quick')})" in said), said
    warned = []
    monkeypatch.setattr(qt.QtWidgets.QMessageBox, "warning", lambda *a: warned.append(a[2]))
    window.open_ui(gone, "../sa", ext, ext.ui["quick"])                   # a name that must not fill a placeholder
    assert warned and warned[0].startswith("Cannot open Quick look for ../sa.\n\nbad device name '../sa'")
    window.live_view(gone, "../../../x")                                 # nor name a log elsewhere
    assert warned[1].startswith("Cannot open the live view of ../../../x.\n\nbad device name '../../../x'")
    assert not (registry.runtime_dir().parent / "x-view.log").exists()


def test_the_uis_of_a_server_s_devices_are_looked_up_at_once(window, monkeypatch):
    """One worker job finds out what each device of a server can open (for a server elsewhere: with
    one discovery for all), asked again only after a while; what came over the network is only compared."""
    window._timer.stop()                                                 # (no scans: nothing else renders)
    calls, renders = [], []
    real = extensions.discover
    monkeypatch.setattr(extensions, "discover", lambda: calls.append(1) or real())
    s = inventory.Server("remote:x", "x", "remote", ["tcp://127.0.0.1:9"])
    scope = {"type": "SimScope", "extension": "insthub-sim", "status": {}, "streams": {}}
    s.probe = {"devices": {"scope": scope, "scope2": scope, "synth": {**scope, "type": "SimSynth"},
                           "box": {**scope, "extension": ["a", "list"]}, "lamp": {**scope, "extension": "lamps"}}}
    assert window.device_uis(s, "box") == (None, [], None)              # not text: no extension, and no crash
    assert window.device_uis(s, "scope") is None                          # being found out ...
    assert spin(5, lambda: window.device_uis(s, "scope") is not None) and calls == [1]  # ... all, in one discovery
    own = extensions.load(extensions.OWN, "insthub")
    assert window.device_uis(s, "scope") == window.device_uis(s, "scope2") == (own, [own.ui["scope"]], None)
    assert window.device_uis(s, "synth") == (own, [], None)
    missing = "extension lamps is not installed on this computer: insthub ext add FOLDER"
    assert window.device_uis(s, "lamp") == (None, [], missing)
    spin(0.3)
    assert calls == [1]                                                  # asked once, however often it is shown
    monkeypatch.setattr(window, "render", lambda: renders.append(1))
    window._uis[s.address] = (-math.inf, *window._uis[s.address][1:])    # a while later: asked again ...
    window.device_uis(s, "scope")
    assert spin(5, lambda: len(calls) == 2) and not spin(0.3, lambda: renders)  # ... and nothing new: nothing to show
    s.probe["devices"]["lamp"] = {**scope, "extension": "insthub-sim"}   # a device that says something new: at once
    assert window.device_uis(s, "lamp") is None and spin(5, lambda: renders) and len(calls) == 3
    assert window.device_uis(s, "lamp") == (own, [own.ui["scope"]], None)


def test_pause_and_resume_from_the_window(window):
    with insthub.Server("t", [Lamp("lamp")]) as server:
        assert spin(5, lambda: word(window, "device", "local:t", "lamp") == "connected")
        window.open_device("local:t", "lamp")
        page = window.device_page
        assert page.pause.text() == "Pause" and page.edit_file.isEnabled() is False   # in this process: no file
        page.pause.click()
        assert spin(5, lambda: word(window, "device", "local:t", "lamp") == "paused")
        assert server.devices["lamp"].connected is False
        assert spin(3, lambda: page.pause.text() == "Resume") and not page.connect_now.isEnabled()
        assert "Paused since" in page.instrument.text() and "by this window" in page.instrument.text()
        view = pages.device_view(window.servers["local:t"], "lamp")
        assert view.status == "paused" and view.icon == "pause" and view.message.startswith("Paused since")
        with pytest.raises(insthub.DevicePaused):
            _ = server.device("lamp").on
        page.pause.click()                                                   # Resume
        assert spin(5, lambda: word(window, "device", "local:t", "lamp") == "connected")
        assert spin(3, lambda: page.pause.text() == "Pause")


class BlinkingLamp(Lamp):
    blinks = 0

    @insthub.command
    def blink(self, times: int = 1) -> int:
        """Blink it."""
        BlinkingLamp.blinks += times
        return times


def test_a_program_knows_its_own_pause_over_tcp_too():
    with insthub.Server("t", [Lamp("lamp")], tcp=0, ipc=False) as server, insthub.connect(server.addresses[-1]) as c:
        paused = c.device("lamp").bus.pause()["paused"]                        # what the window sends: a token
        assert "pid" not in paused and paused["peer"].startswith("127.0.0.1:")
        assert inventory.paused_here(paused) and inventory.paused_by(paused) == "this window"
        c.device("lamp").bus.resume()
        paused = c._t.request("lamp", "$pause", ("experiment.py",), {}).result(5)["paused"]      # no token
        assert not inventory.paused_here(paused) and inventory.paused_by(paused) == "experiment.py"
        c.device("lamp").bus.resume()


def test_the_window_knows_its_own_pause_and_runs_nothing_while_paused(window, monkeypatch):
    asked = []
    monkeypatch.setattr(window, "confirm", lambda *a: asked.append(a[0]) or True)
    with insthub.Server("t", [BlinkingLamp("lamp")], tcp=0) as server:
        assert spin(5, lambda: word(window, "device", "local:t", "lamp") == "connected")
        window.open_device("local:t", "lamp")
        page = window.device_page
        level = page.props.rows["level"]
        assert spin(3, lambda: level.edit.text() == "0.5")
        page.pause.click()
        assert spin(5, lambda: word(window, "device", "local:t", "lamp") == "paused")
        assert spin(3, lambda: "by this window" in page.instrument.text())
        sets = []
        monkeypatch.setattr(page.props, "write", lambda *a: sets.append(a))
        assert spin(3, lambda: level.edit.isReadOnly())
        level.edit.returnPressed.emit()                                      # Enter in a held field: nothing
        page.commands.choose("blink")
        assert not page.commands.run_button.isEnabled()
        page.commands.args.returnPressed.emit()                              # nor in the arguments line
        spin(0.3)
        assert sets == [] and BlinkingLamp.blinks == 0 and "paused" not in page.commands.result.toPlainText()
        page.pause.click()                                                   # Resume: its own pause, no question
        assert spin(5, lambda: word(window, "device", "local:t", "lamp") == "connected")
        assert asked == []
        with insthub.connect(server.addresses[-1]) as other:                 # another program's pause (no token) ...
            other._t.request("lamp", "$pause", ("experiment.py",), {}).result(5)
        assert spin(5, lambda: word(window, "device", "local:t", "lamp") == "paused")
        assert spin(3, lambda: "by experiment.py" in page.instrument.text())
        page.pause.click()                                                   # ... is resumed only when confirmed
        assert spin(5, lambda: asked == ["Resume lamp?"])


def test_restart_of_a_server_started_here_asks_runs_once_and_comes_back(window, monkeypatch):
    class Proc:
        def __init__(self, pid):
            self.pid, self.args = pid, ["python", "-m", "insthub", "serve", "x.toml"]

        def poll(self):
            return None

        def terminate(self):
            time.sleep(0.2)

        def wait(self, timeout=None):
            return 0

    spawned = []
    log = inventory.server_log("x")
    monkeypatch.setattr(manager, "spawn", lambda *a: spawned.append(a) or inventory.Launched(Proc(4343), log, "x"))
    window._spawned.append(inventory.Launched(Proc(4242), log, "x"))
    s = type("S", (), {"service": None, "pid": 4242, "name": "x", "key": "local:x", "origin": "local"})()
    answers = [False, True, True]
    monkeypatch.setattr(window, "confirm", lambda *a: answers.pop(0))
    window.restart_server(s, ask=True)                                     # Cancel: nothing happens
    spin(0.3)
    assert spawned == [] and not window.restarting
    window.restart_server(s, ask=True)
    window.restart_server(s, ask=True)                                     # a second click while it restarts:
    assert spin(3, lambda: len(spawned) == 1) and not spin(0.5, lambda: len(spawned) > 1)
    assert answers == [True]                                               # not even asked
    window._detail = ("server", "local:x")                                 # its page, while it is gone ...
    window.pages.setCurrentWidget(window.server_page)
    window.render()
    assert window.pages.currentWidget() is window.servers_page and window._follow == ("local:x", None)
    opened = []
    monkeypatch.setattr(window, "open_server", opened.append)
    monkeypatch.setattr(window, "servers", {"local:x": type("S", (), {**vars(type(s)), "pid": 4343})()})
    window.render()                                                        # ... comes back once it is up
    assert opened == ["local:x"] and not window.restarting and window._follow is None


def test_a_refused_start_at_login_change_shows_the_real_state_at_once(monkeypatch):
    unit = {"enabled": True, "command": '"python" "-m" "insthub" "serve" "t.toml"', "unit": "u", "name": "t",
            "state": "active", "sub": "running", "pid": os.getpid()}
    monkeypatch.setattr(service, "units", lambda: [unit])
    monkeypatch.setattr(service, "disable", lambda name: (_ for _ in ()).throw(service.BusError("no luck")))
    monkeypatch.setattr(manager.QtWidgets.QMessageBox, "warning", lambda *a: None)
    qt.application()
    with insthub.Server("t", [sim.Loopback()]):
        window = manager.Manager(refresh_ms=1000)
        try:
            assert spin(5, lambda: "local:t" in window.servers and window.servers["local:t"].service)
            window.open_server("local:t")
            page = window.server_page
            page.login.click()                                               # off: refused
            assert spin(3, lambda: not window.login_pending)                 # at once, not seconds later
            assert spin(3, lambda: page.login.isChecked() and page.login.isEnabled())
        finally:
            window.close()
            spin(0.1)


def test_start_at_login_is_switched_on_the_server_page(monkeypatch):
    unit = {"enabled": True, "command": '"python" "-m" "insthub" "serve" "t.toml"', "unit": "u", "name": "t",
            "state": "active", "sub": "running", "pid": os.getpid()}
    monkeypatch.setattr(service, "units", lambda: [unit])
    calls = []
    for action in ("enable", "disable"):
        monkeypatch.setattr(service, action, lambda name, action=action: calls.append((action, name)))
    qt.application()
    with insthub.Server("t", [sim.Loopback()]):
        window = manager.Manager(refresh_ms=150)
        try:
            assert spin(5, lambda: "local:t" in window.servers and window.servers["local:t"].service)
            window.open_server("local:t")
            page = window.server_page
            assert not page.login_row.isHidden() and page.login.isChecked()
            assert "started when you log in" in page.login_row.hint.text()
            page.login.click()                                               # off
            assert not page.login.isEnabled()                                # at once: no second click meanwhile
            assert spin(3, lambda: calls == [("disable", "t")])
            spin(0.3)                                                        # scans still say it is enabled ...
            assert not page.login.isChecked()                                # ... but the switch shows what was asked
            unit["enabled"] = False
            assert spin(3, lambda: not page.login.isChecked() and page.login.isEnabled()
                        and "only when you start it" in page.login_row.hint.text())
            page.login.click()                                               # on again
            assert spin(3, lambda: calls[-1] == ("enable", "t"))
        finally:
            window.close()
            spin(0.1)


EDITED = """# The bench.  Comments like this one must survive an edit.

[server]
name = "bench"   # how clients find it

# a box on the serial port
[devices.box]
driver = "boxdriver:Box"
path = "{folder}"
port = "18"            # a string that looks like a number
gain = 1

[devices.echo]
driver = "insthub.sim:Loopback"
"""


def test_a_config_is_edited_in_place(window, tmp_path):
    (tmp_path / "boxdriver.py").write_text("class Box:\n    def __init__(self, port, gain=1, mode='fast'):\n"
                                           "        pass\n")
    path = tmp_path / "bench.toml"
    path.write_text(EDITED.format(folder=tmp_path))
    dialog = forms.ServerDialog(window, path, select="box")
    assert dialog.windowTitle() == "Edit bench.toml" and dialog.name.text() == "bench"
    assert [dialog.list.item(i).text() for i in range(dialog.list.count())] == ["box", "echo"]
    assert dialog.dev_name.text() == "box" and set(dialog._arg_edits) == {"port", "gain", "mode"}
    assert dialog._arg_edits["port"].text() == "18" and dialog._arg_edits["mode"].placeholderText() == "fast"
    assert not dialog.create.isEnabled()                                     # nothing changed yet
    assert [dialog.then.itemData(i) for i in range(dialog.then.count())] == [dialog.SAVE]   # no server runs it
    dialog._arg_edits["gain"].setText("3")
    dialog._arg_edits["gain"].textEdited.emit("3")
    dialog.open_now.click()                                                  # lazy
    dialog.list.setCurrentRow(1)
    dialog._remove()                                                         # echo goes
    assert dialog.create.isEnabled() and dialog.create.text() == "Save"
    dialog.create.click()
    text = path.read_text()
    assert text.startswith("# The bench.  Comments like this one must survive an edit.\n")
    assert 'name = "bench"   # how clients find it' in text and "# a box on the serial port" in text
    assert 'port = "18"            # a string that looks like a number' in text   # untouched, and still text
    assert "gain = 3\n" in text and "lazy = true" in text and "echo" not in text
    kwargs, devices = config.load(path)
    assert kwargs == {"name": "bench"} and [d.name for d in devices] == ["box"] and not devices[0].open_on_start
    assert "saved" in window.toast.text().lower()


TOUCHY = r"""# a config with values that are easy to spoil
[server]
name = "rig"
tcp = 8765          # an SSH tunnel: this computer only

# the box
[devices.box]
driver = "boxdriver:Box"
path = "."           # relative: the config's folder
port = 5             # text by default, a number here
term = "\r\n"
prefix = ""
label = "  padded  "
when = 2024-01-01
timeout = nan
gains = [1, 2]       # per channel

[devices.box.kwargs]
path = "/dev/serial/by-id/usb-1"   # the driver's own path argument

[devices.odd]        # no driver: insthub could not start it, but it must not block other changes
note = "unfinished"
"""


def _box(tmp_path):
    (tmp_path / "boxdriver.py").write_text("class Box:\n    def __init__(self, port='COM1', term='\\n', prefix='>',"
                                           " label='', when=None, timeout=1.0, path='', mode='a'):\n        pass\n")
    path = tmp_path / "rig.toml"
    path.write_text(TOUCHY)
    return path


def test_editing_writes_only_what_was_changed(window, tmp_path):
    path = _box(tmp_path)
    dialog = forms.ServerDialog(window, path, select="box")
    assert dialog.dev_name.text() == "box" and "port" in dialog._arg_edits        # its folder counts from the file's
    assert dialog._arg_edits["path"].text() == "/dev/serial/by-id/usb-1"          # its own path, from kwargs
    assert "when = 2024-01-01" in dialog.extra.toPlainText()
    assert dialog.reach.currentData() == forms.TCP_HERE and dialog.port.value() == 8765
    assert not dialog.create.isEnabled()                                          # nothing changed: nothing to save
    dialog.name.setText("rig2")                                                   # a change elsewhere ...
    assert dialog.preview.toPlainText() == TOUCHY.replace('name = "rig"', 'name = "rig2"')   # ... changes only it
    dialog.reach.setCurrentIndex(dialog.reach.findData(forms.LOCAL))              # away and back: no change
    dialog.reach.setCurrentIndex(dialog.reach.findData(forms.TCP_HERE))
    assert "tcp = 8765          # an SSH tunnel" in dialog.preview.toPlainText()
    dialog.reach.setCurrentIndex(dialog.reach.findData(forms.NETWORK))            # the port is kept
    assert 'tcp = "0.0.0.0:8765"          # an SSH tunnel' in dialog.preview.toPlainText()
    dialog.reach.setCurrentIndex(dialog.reach.findData(forms.TCP_HERE))
    dialog._arg_edits["mode"].setText("b")
    dialog._arg_edits["mode"].textEdited.emit("b")                                 # one argument of box
    dialog._arg_edits["path"].setText("/dev/ttyS1")
    dialog._arg_edits["path"].textEdited.emit("/dev/ttyS1")
    dialog.dev_name.setText("psu")
    dialog.dev_name.textEdited.emit("psu")                                        # and its name
    dialog.create.click()
    text = path.read_text()
    assert "# the box\n[devices.psu]\n" in text and "[devices.psu.kwargs]" in text and "[devices.box" not in text
    doc = tomllib.loads(text)
    psu = doc["devices"]["psu"]
    assert psu["path"] == "." and psu["kwargs"] == {"path": "/dev/ttyS1"} and psu["mode"] == "b"
    assert psu["port"] == 5 and psu["term"] == "\r\n" and psu["prefix"] == "" and psu["label"] == "  padded  "
    assert str(psu["when"]) == "2024-01-01" and psu["gains"] == [1, 2] and psu["timeout"] != psu["timeout"]
    assert 'gains = [1, 2]       # per channel' in text and doc["server"] == {"name": "rig2", "tcp": 8765}
    assert doc["devices"]["odd"] == {"note": "unfinished"}


def _type(edit, text):
    edit.setText(text)
    edit.textEdited.emit(text)


def _choose(dialog, offer: str):
    """Choose a device an extension offers from the driver list, as a click on it does."""
    row = next(r for r in range(dialog.offers.rowCount()) if dialog.offers.item(r).data(forms._OFFER)[1].name == offer)
    dialog.driver.setCurrentIndex(row)
    dialog.driver.activated.emit(row)


KWARGS = """[devices.box]
driver = "boxdriver:Box"
path = "."
gain = 3
close = "shutdown"     # insthub's: what it calls at the end

[devices.box.kwargs]
port = "COM1"
extra_thing = 1

[devices.echo]
driver = "insthub.sim:Loopback"
lazy = true
reconnect_interval = 9
"""


def _kwargs(tmp_path):
    (tmp_path / "boxdriver.py").write_text(
        "class Box:\n    def __init__(self, port, gain=1, mode='fast', close=None, channels=()):\n        pass\n\n"
        "class Other:\n    def __init__(self, port, speed=1):\n        pass\n")
    path = tmp_path / "k.toml"
    path.write_text(KWARGS)
    return path


def test_arguments_are_written_where_the_file_gives_them(window, tmp_path):
    dialog = forms.ServerDialog(window, _kwargs(tmp_path), select="box")
    e = dialog._arg_edits
    assert e["port"].text() == "COM1" and e["close"].text() == ""           # insthub's close is not the argument
    assert 'close = "shutdown"' in dialog.extra.toPlainText() and "kwargs.extra_thing = 1" in dialog.extra.toPlainText()
    _type(e["port"], "COM2")                                                  # under kwargs, where it was
    box = tomllib.loads(dialog.preview.toPlainText())["devices"]["box"]
    assert box["kwargs"]["port"] == "COM2" and "port" not in box and box["close"] == "shutdown"
    dialog.extra.setPlainText('close = "shutdown"')                           # a kwargs.* line deleted: it goes
    assert "extra_thing" not in dialog.preview.toPlainText() and "port = \"COM2\"" in dialog.preview.toPlainText()
    dialog.extra.setPlainText('close = "shutdown"\nchannels = [1, 2]')        # an argument given there ...
    assert e["channels"].isReadOnly() and "more keys" in e["channels"].placeholderText()   # ... not in its field
    dialog.extra.setPlainText('close = "shutdown"')
    assert not e["channels"].isReadOnly()
    _type(e["gain"], "5")
    dialog.driver.setCurrentText("boxdriver:Other")                           # another driver by mistake ...
    assert "gain" not in tomllib.loads(dialog.preview.toPlainText())["devices"]["box"]
    dialog.driver.setCurrentText("boxdriver:Box")                             # ... and back: nothing lost
    assert dialog._arg_edits["gain"].text() == "5"
    assert tomllib.loads(dialog.preview.toPlainText())["devices"]["box"]["gain"] == 5
    assert "Removed:" in dialog.removed.text() and "extra_thing = 1" in dialog.removed.text()


def test_the_docs_kwargs_path_example_with_a_driver_not_readable_here(window, tmp_path):
    path = tmp_path / "psu.toml"
    path.write_text('[devices.psu]\ndriver = "mydriver:PowerSupply"\npath = "nowhere"\n\n'
                    '[devices.psu.kwargs]\npath = "/dev/serial/by-id/usb-1"\n')
    dialog = forms.ServerDialog(window, path)
    assert dialog.extra.toPlainText() == 'kwargs.path = "/dev/serial/by-id/usb-1"'   # not the driver's folder
    dialog.open_now.click()
    assert not dialog.problem.text() and dialog.create.isEnabled()
    doc = tomllib.loads(dialog.preview.toPlainText())["devices"]["psu"]
    assert doc == {"driver": "mydriver:PowerSupply", "path": "nowhere", "lazy": True,
                   "kwargs": {"path": "/dev/serial/by-id/usb-1"}}


def test_names_freed_in_the_same_edit_can_be_taken(window, tmp_path):
    path = _kwargs(tmp_path)
    dialog = forms.ServerDialog(window, path, select="echo")
    dialog._remove()                                                          # echo goes, and a new echo comes:
    dialog._add()
    _type(dialog.dev_name, "echo")
    dialog.driver.setCurrentText("insthub.sim:SimScope")
    assert tomllib.loads(dialog.preview.toPlainText())["devices"]["echo"] == {"driver": "insthub.sim:SimScope"}
    assert "<code>[devices.echo]</code>" in dialog.removed.text()            # nothing of the old one is kept

    dialog = forms.ServerDialog(window, path, select="echo")
    dialog._remove()
    dialog.list.setCurrentRow(0)
    _type(dialog.dev_name, "echo")                                            # box takes the removed one's name
    assert not dialog.problem.text(), dialog.problem.text()
    text = dialog.preview.toPlainText()
    assert list(tomllib.loads(text)["devices"]) == ["echo"] and "[devices.echo.kwargs]" in text
    assert 'driver = "boxdriver:Box"' in text and "lazy" not in text

    dialog = forms.ServerDialog(window, path, select="box")                   # and two swap their names
    _type(dialog.dev_name, "x")
    dialog.list.setCurrentRow(1)
    _type(dialog.dev_name, "box")
    dialog.list.setCurrentRow(0)
    _type(dialog.dev_name, "echo")
    assert not dialog.problem.text(), dialog.problem.text()
    devices = tomllib.loads(dialog.preview.toPlainText())["devices"]
    assert devices["echo"]["driver"] == "boxdriver:Box" and devices["box"]["driver"] == "insthub.sim:Loopback"
    assert not dialog.removed.text()                                          # renames remove nothing
    dialog.create.click()
    assert tomllib.loads(path.read_text())["devices"] == devices


def test_arguments_stay_in_sight_when_the_driver_changes(window, tmp_path):
    path = _kwargs(tmp_path)
    (tmp_path / "boxdriver.py").write_text(
        "class Box:\n    def __init__(self, port, gain=1, mode='fast', close=None, channels=()):\n        pass\n\n"
        "class BoxV2(Box):\n    pass\n\n"
        "class Other:\n    def __init__(self, port, speed=1):\n        pass\n")
    dialog = forms.ServerDialog(window, path, select="box")
    _type(dialog._arg_edits["gain"], "5")
    dialog.driver.setCurrentText("boxdriver:BoxV2")                           # inherits its constructor:
    assert set(dialog._arg_edits) == {"port", "gain"}                         # what the fields hold stays
    assert dialog._arg_edits["gain"].text() == "5" and "written" in dialog.args_note.text()
    box = tomllib.loads(dialog.preview.toPlainText())["devices"]["box"]
    assert box["gain"] == 5 and box["kwargs"]["port"] == "COM1" and box["driver"] == "boxdriver:BoxV2"
    dialog.driver.setCurrentText("boxdriver:Box")                             # its own fields again
    assert dialog._arg_edits["gain"].text() == "5" and dialog._arg_edits["port"].text() == "COM1"
    assert dialog.extra.toPlainText() == 'close = "shutdown"\nkwargs.extra_thing = 1'   # never rewritten
    dialog.driver.setCurrentText("nomodule:Thing")                            # not readable here: the same
    assert set(dialog._arg_edits) == {"port", "gain"}

    dialog = forms.ServerDialog(window, path, select="box")                   # typed a key at a time ...
    _type(dialog._arg_edits["gain"], "5")
    for i in range(len("boxdriver:"), len("boxdriver:Other") + 1):
        dialog.driver.setCurrentText("boxdriver:Other"[:i])
    typed = dialog.preview.toPlainText()
    dialog = forms.ServerDialog(window, path, select="box")                   # ... or chosen at once: the same
    _type(dialog._arg_edits["gain"], "5")
    dialog.driver.setCurrentText("boxdriver:Other")
    assert typed == dialog.preview.toPlainText()
    assert "gain" not in tomllib.loads(typed)["devices"]["box"] and 'close = "shutdown"' in typed


def test_a_key_given_twice_points_to_the_one_in_effect(window, tmp_path):
    (tmp_path / "boxdriver.py").write_text("class Box:\n    def __init__(self, port, gain=1):\n        pass\n")
    path = tmp_path / "twice.toml"
    path.write_text('[devices.box]\ndriver = "boxdriver:Box"\npath = "."\nport = "COM1"\n'
                    'kwargs = { port = "COM9" }\n')
    dialog = forms.ServerDialog(window, path)
    port = dialog._arg_edits["port"]
    assert port.text() == "COM9" and not port.isReadOnly()                    # kwargs wins: its field is it
    assert dialog.extra.toPlainText() == 'port = "COM1"'


def test_renamed_and_added_under_the_old_name_and_other_small_things(window, tmp_path):
    path = _kwargs(tmp_path)
    dialog = forms.ServerDialog(window, path, select="echo")
    _type(dialog.dev_name, "echo_old")                                        # keep the old one under a new name
    dialog._add()
    _type(dialog.dev_name, "echo")                                            # and add a new one
    dialog.driver.setCurrentText("insthub.sim:SimScope")
    assert not dialog.problem.text(), dialog.problem.text()
    devices = tomllib.loads(dialog.preview.toPlainText())["devices"]
    assert devices["echo_old"]["lazy"] is True and devices["echo"] == {"driver": "insthub.sim:SimScope"}
    assert not dialog.removed.text()

    path.write_text('[devices.device_psu]\ndriver = "insthub.sim:Loopback"\n')
    dialog = forms.ServerDialog(window, path)
    _choose(dialog, "synth")                                                  # an existing device keeps its name
    assert dialog.dev_name.text() == "device_psu"
    dialog._add()
    _choose(dialog, "synth")                                                  # a new device1 is named after it
    assert dialog.dev_name.text() == "synth"

    path.write_text('[server]\nname = "bare"\n')                              # no devices, no [devices]
    dialog = forms.ServerDialog(window, path)
    dialog.name.setText("bare2")
    assert not dialog.removed.text()


def test_a_new_port_keeps_the_address_and_the_preview_keeps_its_place(window, tmp_path):
    path = tmp_path / "n.toml"
    path.write_text('[server]\nname = "n"\ntcp = "192.168.1.5:8765"   # the lab network only\n\n'
                    + "".join(f'[devices.d{i}]\ndriver = "insthub.sim:Loopback"\n\n' for i in range(40)))
    dialog = forms.ServerDialog(window, path, select="d39")
    dialog.resize(1200, 780)
    dialog.show()
    dialog.port.setValue(8766)
    assert 'tcp = "192.168.1.5:8766"   # the lab network only' in dialog.preview.toPlainText()
    dialog.port.setValue(8765)
    bar = dialog.preview.verticalScrollBar()
    dialog.open_now.click()                                                   # a change at the bottom: shown
    assert spin(1, lambda: bar.value() > 0)
    at = bar.maximum()
    bar.setValue(at)
    dialog.extra.setPlainText("reconnect_interval = 2")                       # more, below it: it stays there
    assert bar.value() == at
    bar.setValue(0)
    dialog.name.setText("n2")                                                 # a change at the top: shown
    assert bar.value() == 0
    dialog.list.setCurrentRow(20)
    dialog.open_now.click()                                                   # then one further down: shown too
    assert spin(1, lambda: bar.value() > 0)
    dialog.hide()


def test_saving_when_the_file_vanished_or_changed(window, tmp_path, monkeypatch):
    path = _box(tmp_path)
    warned = []
    monkeypatch.setattr(forms.QtWidgets.QMessageBox, "warning", lambda *a: warned.append(a[2]))
    dialog = forms.ServerDialog(window, path)
    dialog.name.setText("rig2")
    path.unlink()
    dialog.create.click()                                                         # no crash: it says why
    assert warned and "Cannot read" in warned[0] and not path.exists()
    path.write_text(TOUCHY.replace("# the box", "# changed meanwhile"))
    monkeypatch.setattr(window, "confirm", lambda *a: True)                       # Save over them
    shown = dialog.preview.toPlainText()
    dialog.create.click()
    assert path.read_text() == shown                                              # exactly what was shown


def test_only_insthub_configs_are_edited(window, tmp_path):
    assert inventory.served(["python3", "srv.py", "settings.toml"]) == "srv.py"     # its own settings: not ours
    assert inventory.served(["/v/bin/python", "-m", "insthub", "serve", "lab.toml"]) == "lab.toml"
    assert inventory.served(["python3", "-X", "dev", "-W", "ignore", "-u", "srv.py"]) == "srv.py"
    other = tmp_path / "devices.toml"
    other.write_text('[devices]\nscope = "tcp://h:1"\n')
    with pytest.raises(ValueError, match="not an insthub server"):
        forms.ServerDialog(window, other)
    empty = tmp_path / "empty.toml"
    empty.write_text('[server]\nname = "x"\n')
    dialog = forms.ServerDialog(window, empty)                                     # no devices: none made up
    assert dialog.devices == [] and not dialog.device_editor.isEnabled()
    dialog.name.setText("y")
    assert 'name = "y"' in dialog.preview.toPlainText() and dialog.create.isEnabled()


def test_editing_restarts_the_server_that_reads_the_config(window, tmp_path, monkeypatch):
    path = tmp_path / "edited.toml"
    path.write_text('[server]\nname = "edited"\n\n[devices.scope]\ndriver = "insthub.sim:SimScope"\n')
    window.start_server([sys.executable, "-m", "insthub", "serve", str(path)], "edited", cwd=tmp_path)
    try:
        assert spin(15, lambda: word(window, "device", "local:edited", "scope") == "connected")
        s = window.servers["local:edited"]
        assert window.started_here(s.pid) and inventory.server_file(s) == path
        opened = []
        monkeypatch.setattr(forms.ServerDialog, "exec", lambda self: opened.append(self))
        window.edit_config("local:edited", "scope")                         # what "Edit in its config..." does
        dialog, = opened
        assert dialog.then.currentData() == dialog.RESTART and dialog.create.text().startswith("Save")
        dialog._arg_edits["points"].setText("64")
        dialog._arg_edits["points"].textEdited.emit("64")
        assert dialog.create.text() == "Save and restart"
        first = s.pid
        dialog.create.click()
        assert spin(20, lambda: "local:edited" in window.servers and window.servers["local:edited"].pid
                    not in (None, first) and word(window, "device", "local:edited", "scope") == "connected")
        assert "points = 64" in path.read_text()
        assert insthub.device("scope").get_waveform().volts.shape == (64,)   # the new config is in effect
    finally:
        for proc in window._spawned:
            proc.process.terminate()
            proc.process.wait(10)


def test_editing_a_service_config_starts_it(window, monkeypatch, tmp_path):
    path = tmp_path / "rig.toml"
    path.write_text('[devices.echo]\ndriver = "insthub.sim:Loopback"\n')
    monkeypatch.setattr(service, "units", lambda: [{"name": "rig", "state": "inactive", "sub": "dead", "pid": None,
                                                    "enabled": False, "unit": "u",
                                                    "command": f'"python" "-m" "insthub" "serve" "{path}"'}])
    started = []
    monkeypatch.setattr(service, "start", lambda name: started.append(name))
    assert spin(5, lambda: "service:rig" in window.servers)
    dialog = forms.ServerDialog(window, path, server=window.servers["service:rig"])
    assert dialog.then.currentData() == dialog.RESTART and "Start the service" in dialog.then.currentText()
    dialog.open_now.click()
    assert dialog.create.text() == "Save and start"
    dialog.create.click()
    assert spin(3, lambda: started == ["rig"]) and "lazy = true" in path.read_text()


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
        item = dialog.list.item(0)
        if "runs here" in item.text():                                  # found here first: not ticked for you
            assert item.checkState() == qt.QtCore.Qt.CheckState.Unchecked and not dialog.add.isEnabled()
            item.setCheckState(qt.QtCore.Qt.CheckState.Checked)
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


def test_extension_folders_are_added_and_removed_in_settings(window, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("INSTHUB_EXTENSIONS")
    good, broken = tmp_path / "good", tmp_path / "broken"
    for folder, api in ((good, 1), (broken, 9)):
        (folder / ".venv" / "bin").mkdir(parents=True)
        (folder / ".venv" / "bin" / "python").write_text("")
        (folder / "insthub-extension.toml").write_text(f'[extension]\nid = "{folder.name}"\ntitle = "The '
                                                       f'{folder.name} one"\napi = {api}\n')
    page = window.settings_page
    rows = lambda: [(r.findChildren(qt.QtWidgets.QLabel)[0].text(), r.hint.text())
                    for r in (page.ext_rows.itemAt(i).widget() for i in range(page.ext_rows.count()))]
    window.show_page("settings")
    assert spin(3, lambda: [t for t, _ in rows()] == ["Simulated instruments"])
    assert "comes with insthub" in rows()[0][1]
    for folder in (good, broken):
        monkeypatch.setattr(qt.QtWidgets.QFileDialog, "getExistingDirectory", lambda *a, f=folder: str(f))
        page._add_extension()
    assert spin(3, lambda: [t for t, _ in rows()] == ["Simulated instruments", "The good one", "The broken one"])
    assert "in your list" in rows()[1][1] and "written for a newer insthub" in rows()[2][1]  # broken: says why
    remove = page.ext_rows.itemAt(1).widget().findChildren(qt.QtWidgets.QPushButton)[0]
    remove.click()
    assert spin(3, lambda: [t for t, _ in rows()] == ["Simulated instruments", "The broken one"])
    assert extensions.listed_folders() == [str(broken)]


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


def test_the_log_of_a_server_without_a_journal_or_a_log_file(window, monkeypatch):
    with insthub.Server("t", [sim.Loopback()], tcp=0):                     # started by hand: no log file here
        assert spin(5, lambda: word(window, "server", "local:t") == "running")
        logging.getLogger("insthub.loopback").warning("something to read in the window")
        window.open_server("local:t")
        page = window.server_page
        assert spin(3, lambda: not page.logs.isHidden())
        page.logs.click()
        dialog = window._logs["logs:" + window.servers["local:t"].address]
        assert spin(5, lambda: "WARNING insthub.loopback: something to read in the window" in dialog.text.toPlainText())
        dialog.hide()
    monkeypatch.setattr(insthub.server, "FEATURES", insthub.server.FEATURES - {"logs"})   # an older insthub
    with insthub.Server("old", [sim.Loopback()], tcp=0):
        assert spin(5, lambda: word(window, "server", "local:old") == "running")
        window.open_server("local:old")
        assert spin(3, lambda: window.server_page.server.probe is not None) and window.server_page.logs.isHidden()


def test_a_server_started_here_that_fails_says_why(window, monkeypatch):
    said = []
    monkeypatch.setattr(window, "say", lambda text, ms=5000: said.append(text))
    code = "import sys; print('error: [devices.psu] needs a driver'); sys.exit(1)"
    window.start_server([sys.executable, "-c", code], "broken")
    assert spin(10, lambda: any("broken ended with an error: error: [devices.psu] needs a driver" in t
                                for t in said)), said


def test_a_device_is_added_from_its_servers_page_and_tiles_say_where_they_are(window, monkeypatch, tmp_path):
    path = tmp_path / "rig.toml"
    path.write_text('# the rig\n[devices.echo]\ndriver = "insthub.sim:Loopback"\n')
    unit = {"name": "rig", "state": "inactive", "sub": "dead", "pid": None, "enabled": False, "unit": "u",
            "command": f'"python" "-m" "insthub" "serve" "{path}"'}
    monkeypatch.setattr(service, "units", lambda: [unit])
    started = []
    monkeypatch.setattr(service, "start", lambda name: started.append(name))
    opened = []
    monkeypatch.setattr(forms.ServerDialog, "exec", lambda self: opened.append(self))
    assert spin(5, lambda: "service:rig" in window.servers)
    window.open_server("service:rig")
    page = window.server_page
    assert not page.add_tile.isHidden() and "rig.toml" in page.add_tile.detail
    page.add_tile.clicked.emit()                                               # the tile ...
    dialog, = opened
    assert dialog.windowTitle() == "Add a device to rig.toml"
    assert [d.name for d in dialog.devices] == ["echo", "device1"] and dialog.current().name == "device1"
    dialog.driver.setCurrentText("insthub.sim:SimScope")
    assert dialog.dev_name.text() == "scope"                                   # a new device1 is named after it
    _type(dialog.dev_description, "the one on the optics table")
    dialog.create.click()
    doc = tomllib.loads(path.read_text())
    assert path.read_text().startswith("# the rig\n") and doc["devices"]["echo"] == {"driver": "insthub.sim:Loopback"}
    assert doc["devices"]["scope"] == {"driver": "insthub.sim:SimScope", "description": "the one on the optics table"}
    assert spin(3, lambda: started == ["rig"])                                 # ... then it starts
    window.add_device_dialog()                                                 # ... and the Add menu's item
    assert len(opened) == 2 and opened[1].current().was is None
    reopened = forms.ServerDialog(window, path)
    assert reopened.dev_description.text() == "" and reopened.devices[1].description == "the one on the optics table"


class Scpi:
    """SCPI class used to access an instrument over the network."""

    def __init__(self, host, port=5000):
        pass


def test_tiles_say_where_each_device_is_and_what_it_is(window):
    rp = insthub.wrap(Scpi, "rp", kwargs={"host": "10.0.0.5"})
    rp.description = "the one on the optics table"
    with insthub.Server("t", [rp, insthub.wrap(Scpi, "rp2", kwargs={"host": "10.0.0.6"})]):
        assert spin(5, lambda: "local:t" in window.servers and window.servers["local:t"].device("rp2"))
        s = window.servers["local:t"]
        view = pages.device_view(s, "rp")
        assert view.subtitle == "Scpi · 10.0.0.5 · t" and view.message == "the one on the optics table"
        assert pages.device_view(s, "rp", server_shown=False).subtitle == "Scpi · 10.0.0.5"   # on its server's page
        assert pages.device_view(s, "rp2").message == Scpi.__doc__                # no description: its docstring
        window.open_device("local:t", "rp")
        assert "10.0.0.5" in window.device_page.header.subtitle.text()
        window.show_page("devices")
        window.devices_page.search.setText("10.0.0.6")                           # found by its address
        tiles = window.devices_page.tiles
        assert spin(3, lambda: not tiles["local:t", "rp2"].isHidden() and tiles["local:t", "rp"].isHidden())


def test_the_dialog_warns_when_a_driver_has_a_python_of_its_own(window, monkeypatch, tmp_path):
    drv = tmp_path / "drv"
    (drv / ".venv" / "bin").mkdir(parents=True)
    (drv / ".venv" / "bin" / "python").write_text("")
    (drv / "box.py").write_text("class Box:\n    def __init__(self, port):\n        pass\n")
    path = tmp_path / "rig.toml"
    path.write_text('[devices.echo]\ndriver = "insthub.sim:Loopback"\n')
    monkeypatch.setattr(service, "units", lambda: [{"name": "rig", "state": "inactive", "sub": "dead", "pid": None,
                                                    "enabled": False, "unit": "u",
                                                    "command": f'"/srv/py/bin/python" "-m" "insthub" "serve" "{path}"'}])
    assert spin(5, lambda: "service:rig" in window.servers)
    dialog = forms.ServerDialog(window, path, server=window.servers["service:rig"], add=True)
    assert dialog.driver_note.isHidden()
    dialog.dev_folder.setText(str(drv))
    dialog.driver.setCurrentText("box:Box")
    assert not dialog.driver_note.isHidden() and str(drv / ".venv" / "bin" / "python") in dialog.driver_note.text()
    assert "/srv/py/bin/python" in dialog.driver_note.text() and "New server" in dialog.driver_note.text()


def test_a_driver_without_arguments_does_not_get_the_last_ones(window):
    dialog = forms.ServerDialog(window)                                       # New server: SimScope
    _type(dialog._arg_edits["points"], "2")
    dialog.driver.setCurrentText("insthub.sim:Loopback")                      # takes none: nothing of SimScope's
    assert dialog._arg_edits == {} and "takes no arguments" in dialog.args_note.text()
    assert "points" not in dialog.preview.toPlainText()


def test_the_preview_marks_the_right_line_with_unusual_characters(window, tmp_path):
    path = tmp_path / "odd.toml"
    path.write_text('# a no-break space and a line separator\n[devices.box]\ndriver = "insthub.sim:SimScope"\n'
                    'points = 100\n')
    dialog = forms.ServerDialog(window, path)
    _type(dialog._arg_edits["points"], "64")
    marks = dialog.preview.extraSelections()
    assert [m.cursor.block().text() for m in marks] == ["points = 64"]


def test_a_restarted_server_is_followed_under_a_new_key(window, monkeypatch):
    class S:
        def __init__(self, key, pid, unit=None, devices=("echo",)):
            self.key, self.name, self.pid, self.origin = key, key.split(":")[1], pid, key.split(":")[0]
            self.service, self.devices = ({"name": unit} if unit else None), list(devices)
            self.probe = self.error = None

        def device_names(self):
            return self.devices

    for page in (window.sidebar, window.devices_page, window.servers_page):
        monkeypatch.setattr(page, "sync", lambda *a, **kw: None)
    shown = []
    monkeypatch.setattr(window.server_page, "show_server", lambda s: shown.append(s.key))
    window.restarting["service:rig"] = {"key": "service:rig", "old": None, "proc": None, "unit": "rig"}
    window._detail = ("server", "service:rig")                               # Save and start of a stopped service
    window.pages.setCurrentWidget(window.server_page)
    monkeypatch.setattr(window, "servers", {"local:rig": S("local:rig", 99, unit="rig")})
    window.render()
    assert window._detail == ("server", "local:rig") and shown == ["local:rig"] and not window.restarting

    opened = []                                                               # from a device's page: back to it
    monkeypatch.setattr(window, "open_device", lambda key, name: opened.append((key, name)))
    window.restarting["local:rig"] = {"key": "local:rig", "old": 99, "proc": None, "unit": "rig"}
    window._detail = ("device", "local:rig", "echo")
    window.pages.setCurrentWidget(window.device_page)
    monkeypatch.setattr(window, "servers", {})
    window.render()
    assert window.pages.currentWidget() is window.devices_page and window._follow == ("local:rig", "echo")
    monkeypatch.setattr(window, "servers", {"local:rig": S("local:rig", 100, unit="rig")})
    window.render()
    assert opened == [("local:rig", "echo")] and window._follow is None

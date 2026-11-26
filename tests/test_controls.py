"""The controls of any device (insthub.controls), as a panel embeds them: on a proxy, run without a
display.  The window's device page is made of the same ones (tests/test_manager.py)."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("insthub.qt", exc_type=ImportError)  # needs a Qt binding

import insthub
from insthub import controls, qt, sim
from insthub import widgets as ui


def spin(seconds, until=lambda: False):
    app = qt.application()
    end = time.monotonic() + seconds
    while time.monotonic() < end and not until():
        app.processEvents()
        time.sleep(0.01)
    return until()


class Rig(insthub.Device):
    """A rig whose settings say what they take."""

    def open(self):
        self.values, self.fired = {"mode": "CW", "level": 2.5, "gain": "auto", "on": False}, 0

    def _get(name):
        return lambda self: self.values[name]

    def _set(name):
        return lambda self, value: self.values.__setitem__(name, value)

    mode = insthub.prop(_get("mode"), _set("mode"), choices=["CW", "pulsed"])
    level = insthub.prop(_get("level"), _set("level"), unit="V", min=0, max=10, dangerous=True)
    gain = insthub.prop(_get("gain"), _set("gain"), unit="dB", min=0, max=40)
    on = insthub.prop(_get("on"), _set("on"))

    @insthub.command(dangerous=True, params={"ms": {"unit": "ms", "min": 0}})
    def fire(self, ms=10):
        self.fired += 1
        return ms

    @insthub.command
    def identify(self) -> str:
        return "rig 1"


@pytest.fixture
def rig():
    qt.application()
    with insthub.Server("c", [Rig("rig"), sim.SimScope()], tcp=0) as server:
        yield server, insthub.connect(server.addresses[-1])


def test_a_panel_gets_the_settings_and_commands_of_its_device(rig, monkeypatch):
    server, client = rig
    values = server.devices["rig"].values
    box = controls.Controls(client.rig)
    assert [box.tabText(i) for i in range(2)] == ["Settings (4)", "Commands (2)"]
    rows = box.settings.rows
    assert list(rows) == ["gain", "level", "mode", "on"]                    # read when shown
    assert spin(3, lambda: all(row.loaded for row in rows.values()))
    assert rows["level"].editor is rows["level"].number and rows["mode"].editor is rows["mode"].choice
    changed = []
    box.settings.changed.connect(lambda name, value: changed.append((name, value)))
    rows["on"].switch.click()                                               # changed in place
    assert spin(3, lambda: values["on"] is True and changed == [("on", True)])

    asked = []
    monkeypatch.setattr(ui, "confirm", lambda parent, title, *a: asked.append(title) or False)
    rows["level"].number.setValue(4)
    rows["level"].apply()                                                   # dangerous: asked, and declined
    assert asked == ["Set rig.level?"] and rows["level"].number.value() == 2.5
    spin(0.2)
    assert values["level"] == 2.5

    commands = box.commands
    commands.choose("identify")
    commands.run()
    assert spin(3, lambda: commands.result.toPlainText() == "rig 1")
    commands.choose("fire")
    commands.args.setText("ms=5")
    commands.run()                                                          # declined too
    assert asked[-1] == "Run rig.fire?" and server.devices["rig"].fired == 0
    monkeypatch.setattr(ui, "confirm", lambda *a: True)
    commands.run()
    assert spin(3, lambda: commands.result.toPlainText() == "5") and server.devices["rig"].fired == 1


def test_include_and_exclude_choose_what_is_shown(rig):
    _, client = rig
    some = controls.Controls(client.rig, include=["on", "identify", "level", "nosuch"])
    assert some.settings.shown == ["on", "level"] and some.commands.shown == ["identify"]  # in include's order
    others = controls.Controls(client.rig, exclude=["gain", "fire"])
    assert others.settings.shown == ["level", "mode", "on"] and others.commands.shown == ["identify"]
    scope = controls.Controls(client.scope, exclude=["live"])
    assert not scope.isTabVisible(0) and scope.tabText(1) == "Commands (5)"  # no properties: no Settings tab
    every = controls.Controls(client.rig, include=None, exclude=None)       # None: all of them
    assert every.settings.shown == ["gain", "level", "mode", "on"] and every.commands.shown == ["fire", "identify"]
    one = controls.Controls(client.rig, include="identify")                 # one name, not its letters
    assert one.settings.shown == [] and one.commands.shown == ["identify"]
    assert controls.Controls(client.rig, exclude="fire").commands.shown == ["identify"]


def test_what_fails_is_said_where_it_was_asked(rig):
    server, client = rig
    box = controls.Controls(client.rig)
    mode = box.settings.rows["mode"]
    assert spin(3, lambda: mode.loaded)
    server.stop()                                                           # its server is gone: no call starts
    mode.choice.activated.emit(1)
    assert spin(3, lambda: "ConnectionLost" in mode.message.text())                # said under it, not raised
    box.commands.choose("identify")
    box.commands.run()
    assert spin(3, lambda: box.commands.result.toPlainText().startswith("ConnectionLost: "))
    assert "failed after" in box.commands.took.text()
    error = qt.QtGui.QPalette.ColorRole.WindowText
    mode.message.ensurePolished()                                           # in red, also outside the window
    assert mode.message.palette().color(error).name() == ui.STATUS["bad"]


def test_a_server_that_takes_long_to_reach_does_not_hold_up_the_gui(rig, monkeypatch):
    from insthub import client as insthub_client

    server, client = rig
    box = controls.Controls(client.rig)
    rows = box.settings.rows.values()
    assert spin(3, lambda: all(row.loaded for row in rows))
    server.stop()
    spin(0.3)

    def unanswered(self, timeout):                                          # a computer that is off: no answer
        time.sleep(0.5)
        raise insthub.ConnectionLost(f"cannot connect to {self.address}: timed out")
    monkeypatch.setattr(insthub_client._SocketTransport, "_connect", unanswered)
    t = time.monotonic()
    box.settings.read_all()                                                 # Reload: four properties
    assert time.monotonic() - t < 0.2
    assert spin(3, lambda: all(not row.message.isHidden() for row in rows))
    assert time.monotonic() - t < 1.5                                       # tried once, not once each
    assert all("timed out" in row.message.text() for row in rows)


def test_the_same_device_keeps_what_is_typed_and_the_result_on_its_way(rig):
    _, client = rig
    commands = controls.Commands(client.rig)
    commands.choose("fire")
    commands.args.setText("ms=7")
    commands.confirm = lambda *a: True
    commands.run()
    commands.load(None)                                                     # it could not be described just now
    assert not commands.run_button.isEnabled()
    commands.load(client.rig.bus.describe())                                # ... and it is back
    assert commands.current() == "fire" and commands.args.text() == "ms=7"
    assert spin(3, lambda: commands.result.toPlainText() == "7")            # its result is shown all the same
    assert commands.run_button.isEnabled()


def test_load_shows_the_device_as_it_is_now(rig):
    _, client = rig
    box = controls.Controls(client.rig)
    rows, commands = box.settings.rows, box.commands
    assert spin(3, lambda: all(row.loaded for row in rows.values()))
    client.rig.bus.pause()
    box.load(client.rig.bus.describe())                                     # paused: nothing offered
    commands.choose("identify")
    assert rows["gain"].edit.isReadOnly() and not commands.run_button.isEnabled()
    assert not box.settings.paused_note.isHidden()
    client.rig.bus.resume()
    box.load(client.rig.bus.describe())
    assert not rows["gain"].edit.isReadOnly() and commands.run_button.isEnabled()
    assert box.settings.rows is rows                                        # the same rows, read again


class Sluggish(insthub.Device):
    """A property that takes a while to read."""

    @insthub.prop
    def level(self) -> float:
        time.sleep(0.2)
        return 1.0


def test_controls_that_are_gone_get_no_answers(monkeypatch):
    import gc
    import sys

    qt.application()
    shown, raised = [], []
    monkeypatch.setattr(controls._PropRow, "show_value", lambda row, *a, **k: shown.append(a))
    monkeypatch.setattr(controls._PropRow, "show_error", lambda row, *a, **k: shown.append(a))
    monkeypatch.setattr(sys, "excepthook", lambda *exc: raised.append(exc))  # (else PyQt aborts the program)
    with insthub.Server("c", [Sluggish("slow")], tcp=0) as server:
        host = qt.QtWidgets.QWidget()
        controls.Controls(insthub.connect(server.addresses[-1]).slow, parent=host)  # reading its level ...
        del host
        gc.collect()                                                        # ... when it closes for good
        spin(0.5)
    assert shown == [] and raised == []                                     # the reply reached nothing

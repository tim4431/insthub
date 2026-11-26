"""The reference extension, examples/extension (the demo laser): insthub finds it, checks it, builds
its device from its server file and from its manifest, and shows its panel and its app, also in
programs of their own, as `insthub open` starts them (see docs/design.md#extensions)."""
import importlib
import os
import shlex
import shutil
import sys
import time
import types
import warnings
from pathlib import Path

import pytest

import insthub
from insthub import config, extensions, sim
from insthub.errors import UnknownCommand

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "extension"


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """A copy of the reference extension, first on sys.path.  What a test imports from it is
    forgotten afterwards, so the next test's copy is not taken for another folder's modules of the
    same names."""
    folder = shutil.copytree(EXAMPLE, tmp_path / "extension",
                             ignore=shutil.ignore_patterns("__pycache__", ".venv"))
    monkeypatch.syspath_prepend(str(folder))  # (also puts sys.path back afterwards)
    yield folder
    for name in [n for n in sys.modules if n.split(".")[0] in ("demolaser", "demolaser_insthub")]:
        del sys.modules[name]


@pytest.fixture
def demo_venv(demo):
    """The copy with its own virtualenv's python, as after `uv venv` there: a stand-in that runs
    this Python, for what insthub runs in the extension's Python."""
    if os.name == "nt":
        pytest.skip("the stand-in python is a shell script")
    python = demo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o755)
    return demo


def spin(seconds, until=lambda: False):
    from insthub import qt

    app = qt.application()
    end = time.monotonic() + seconds
    while time.monotonic() < end and not until():
        app.processEvents()
        time.sleep(0.01)
    return until()


def laser_server(name="demo-laser", **kwargs):
    from demolaser_insthub.device import Laser

    return insthub.Server(name, [Laser("laser")], **kwargs)


@pytest.mark.usefixtures("demo_venv")  # (it runs the extension's Python)
def test_insthub_finds_the_reference_extension_and_finds_nothing_wrong(demo, monkeypatch):
    monkeypatch.setenv(extensions.ENV, str(demo))
    own, ext = extensions.discover()
    assert own.id == "insthub-sim" and ext.usable and ext.source == "added" and ext.folder == demo
    assert (ext.id, ext.title, ext.version) == ("demo-laser", "Demo laser", "0.1")
    assert ext.python == str(demo / ".venv" / "bin" / "python")  # the virtualenv in its folder
    assert list(ext.devices) == ["laser"]
    assert ext.devices["laser"].doc == "a simulated diode laser, no hardware needed"
    assert {name: ui.kind for name, ui in ext.ui.items()} == {"panel": "panel", "app": "app"}
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # not even a warning: its device's version is the manifest's, ...
        assert extensions.check(demo) == []
        assert extensions.check(demo, import_=True) == []  # its driver, imported in its own Python: no Qt
        assert extensions.check(demo / "laser.toml", import_=True) == []  # as `insthub service install` does


def test_its_server_file_and_its_manifest_make_the_laser(demo, monkeypatch):
    monkeypatch.chdir(demo.parent)  # (any folder: `path = "."` counts from the file's)
    server, [laser] = config.load(demo / "laser.toml")
    assert server == {"name": "demo-laser"}
    ext = extensions.load(demo / extensions.MANIFEST)
    table = ext.device_table(ext.devices["laser"])  # what New server writes for it
    assert table == {"driver": "demolaser_insthub.device:Laser", "path": str(demo), "extension": "demo-laser",
                     "port": "sim"}
    assert type(config.build_device("laser", table)) is type(laser)
    with insthub.Server("demo-laser", [laser]) as srv:
        dev = srv.device("laser")
        d = dev.bus.describe()
        assert (d["type"], d["extension"], d["extension_version"], d["simulated"]) == \
            ("DemoLaser", "demo-laser", "0.1", True)
        assert d["properties"]["power"]["meta"] == {"unit": "mW", "min": 0, "max": 50.0, "step": 0.1}
        assert d["properties"]["mode"]["meta"] == {"choices": ["cw", "pulsed"]}  # from its annotation
        assert d["properties"]["emission"]["dangerous"] and d["commands"]["set_emission"]["dangerous"]
        assert d["commands"]["pulse"]["dangerous"]
        assert d["commands"]["pulse"]["params"]["ms"] == {"unit": "ms", "min": 0.1, "max": 1000.0}  # what it takes
        assert {"poll:temperature", "poll:power"} <= set(d["tasks"])
        with pytest.raises(UnknownCommand):
            dev.bus.call("write", "EMIT 1")  # the raw protocol (it gets around the marks): gone
        dev.power = 60
        assert dev.power == 50.0  # the laser clamps; the metadata only advises
        with dev.bus.subscribe("power", retained=False) as power:  # read every second, and published
            assert power.get(timeout=3).data == 50.0


def test_its_driver_never_leaves_the_beam_on(demo, monkeypatch):
    """A pulse it refuses never turns the beam on, and one cut short turns it off again."""
    import demolaser

    laser = demolaser.DemoLaser()
    laser.set_mode("pulsed")
    for ms in (-1, 0, 1e9, float("nan")):
        with pytest.raises(ValueError, match=r"a pulse of .* ms is outside 0.1 to 1000 ms"):
            laser.pulse(ms)
        assert laser.get_emission() is False
    laser.pulse(1)
    assert laser.get_emission() is False

    def interrupted(seconds):  # as Ctrl-C does during the pulse
        assert laser.get_emission() is True
        raise KeyboardInterrupt

    monkeypatch.setattr(demolaser, "time", types.SimpleNamespace(sleep=interrupted, monotonic=time.monotonic))
    with pytest.raises(KeyboardInterrupt):
        laser.pulse(10)
    assert laser.get_emission() is False


def test_its_panel_sends_the_power_and_shows_what_the_laser_has(demo, monkeypatch):
    qt = pytest.importorskip("insthub.qt", exc_type=ImportError)
    import demolaser
    from demolaser_insthub.ui import panel

    with laser_server() as server:
        laser = server.devices["laser"]
        window = qt.device_window(panel.make, insthub.connect(server.addresses[0]).laser, "Laser panel")
        window.show()
        shown = window.centralWidget()
        assert window.windowTitle() == "Laser panel - laser"
        assert shown.slider.maximum() == 500  # 50 mW in steps of 0.1 mW: the laser's own limits
        assert spin(5, lambda: shown.power.text() == "1 mW" and shown.temperature.text() == "25.00 °C")
        shown.slider.setValue(125)  # sent (through qt.coalesce) ...
        assert spin(3, lambda: laser.target.get_power() == 12.5 and shown.power.text() == "12.5 mW")
        server.device("laser").power = 60  # ... and shown when changed elsewhere, as the laser took it
        assert spin(5, lambda: shown.power.text() == "50 mW") and shown.slider.value() == 500
        took = demolaser.DemoLaser.set_power

        def slowly(self, mw):  # as a laser does that takes half a second to take a power
            time.sleep(0.5)
            return took(self, mw)

        monkeypatch.setattr(demolaser.DemoLaser, "set_power", slowly)
        shown.slider.setValue(300)  # while the laser takes 30 mW ...
        laser.publish("power", 7.0)  # ... what it reported before is not shown over it
        spin(0.3)
        assert shown.set_power.busy and shown.power.text() == "50 mW"
        assert spin(3, lambda: shown.power.text() == "30 mW")  # what it took, once it has

        def refuse(self, mw):
            raise ValueError("interlock open")

        monkeypatch.setattr(demolaser.DemoLaser, "set_power", refuse)
        shown.slider.setValue(100)  # refused: said, and the power it has is shown again
        assert spin(3, lambda: "interlock open" in shown.message.text())
        assert shown.slider.value() == 300 and shown.power.text() == "30 mW"
        monkeypatch.setattr(demolaser.DemoLaser, "set_power", took)
        shown.slider.setValue(100)
        assert spin(3, lambda: shown.power.text() == "10 mW") and shown.message.text() == ""

        def read_first(self, mw):  # as when it reads its power (every second) just before it takes one
            laser.publish("power", self.get_power())
            return took(self, mw)

        monkeypatch.setattr(demolaser.DemoLaser, "set_power", read_first)
        shown.slider.setValue(200)  # what it read before it took 20 mW comes with the answer ...
        assert spin(3, lambda: not shown.set_power.busy and shown.power.text() == "20 mW")
        labels = set()  # what the panel shows over 0.6 s, while it looks at the reports 5 times a second
        spin(0.6, lambda: labels.add(shown.power.text()))
        assert labels == {"20 mW"} and shown.slider.value() == 200  # ... and is not shown after it
        monkeypatch.setattr(demolaser.DemoLaser, "set_power", took)
        assert shown.controls.settings.shown == ["emission", "wavelength", "mode"]  # the rest: Controls
        assert shown.controls.commands.shown == ["pulse", "identify"]
        window.close()  # the panel closes with its window ...
        assert spin(3, lambda: laser.subscribers() == 0) and shown.watchers == []  # ... and watches nothing


def test_its_app_only_attaches_given_an_address_and_else_hosts_one(demo, monkeypatch, capsys):
    qt = pytest.importorskip("insthub.qt", exc_type=ImportError)
    app = importlib.import_module("demolaser_insthub.ui.__main__")
    seen = []

    def run(application=None):  # instead of the event loop: what the app shows, and which servers run here
        windows = [w for w in qt.QtWidgets.QApplication.topLevelWidgets() if w.isVisible()]
        servers = sorted(s.name for s in insthub.server.local_servers())
        seen.append(([w.statusBar().currentMessage() for w in windows], servers))
        for window in windows:
            window.close()
        return 0

    monkeypatch.setattr(qt, "run", run)
    assert app.main([]) == 0  # no laser runs: the app hosts one, for as long as it is open
    assert seen.pop() == (["laser is hosted by this program: scripts reach it while it is open"], ["laser"])
    assert insthub.server.local_servers() == []
    with laser_server(tcp=0) as server, insthub.Server("other", [sim.SimScope("scope")], tcp=0) as other:
        address, elsewhere = server.addresses[-1], other.addresses[-1]
        assert app.main(["--address", address, "--name", "laser"]) == 0  # given an address: only attached
        assert seen.pop() == ([f"laser at {address}"], ["demo-laser", "other"])
        assert app.main([]) == 0  # the laser runs: found by its name, and attached to
        assert seen.pop()[1] == ["demo-laser", "other"]
        assert app.main(["--address", address, "--name", "nosuch"]) == 1  # never one of its own
        assert app.main(["--address", elsewhere, "--name", "scope"]) == 1
        assert app.main(["--address", "bad addr", "--name", "laser"]) == 1  # said, without a traceback
        err = capsys.readouterr().err
        assert "error: " in err and "nosuch" in err
        assert f"error: scope at {elsewhere} is not a demo laser (its extension: insthub-sim)" in err
        assert "error: bad address 'bad addr': expected tcp://host:port" in err
    assert seen == []


@pytest.mark.usefixtures("demo_venv")  # (it runs the extension's Python)
def test_insthub_opens_its_panel_and_its_app_in_programs_of_their_own(demo, monkeypatch):
    pytest.importorskip("insthub.qt", exc_type=ImportError)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with laser_server() as server, insthub.connect(server.addresses[0]) as client:
        address = server.addresses[0]
        ext, uis, why = extensions.device_uis(client.laser.bus.describe(), address)
        assert why is None and ext.folder == demo and ext.source == "device"  # from the laser's own folders
        assert [ui.name for ui in uis] == ["panel", "app"]

        def connected(launched):
            return launched.pid in [c["pid"] for c in client.info()["clients"]]

        for ui in uis:
            launched = extensions.launch(ext, ui.name, "laser", address)
            try:
                end = time.monotonic() + 30
                while time.monotonic() < end and launched.running and not connected(launched):
                    time.sleep(0.05)
                assert launched.failure() is None and launched.running, launched.log.read_text()
                assert connected(launched)  # it reached the laser
            finally:
                launched.process.terminate()
                launched.process.wait(10)

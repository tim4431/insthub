"""The Qt helpers and the generic viewer, run without a display."""
import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("pyqtgraph")

import insthub  # noqa: E402
from insthub import qt, sim, viewer  # noqa: E402


class Multi(insthub.Device):
    @insthub.periodic(0.02)
    def _go(self):
        t = np.linspace(0, 1, 200)
        self.publish("traces", {"a": np.sin(6 * t), "b": np.cos(6 * t)}, dt=1 / 200, unit="V")
        self.publish("power", 1.5, unit="mW")
        self.publish("note", "hello")


def spin(app, seconds, until=lambda: False):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not until():
        app.processEvents()
        time.sleep(0.01)
    return until()


def test_watch_and_deliver_run_in_the_gui_thread():
    import threading

    app = qt.application()
    with insthub.Server("g", [sim.SimScope(), sim.Loopback()]) as server:
        scope, lb = server.device("scope"), server.device("loopback")
        frames, results, errors, threads = [], [], [], set()
        watcher = qt.watch(scope.subscribe("ch1"), lambda f: (frames.append(f), threads.add(threading.get_ident())))
        qt.deliver(lb.echo.future(41), lambda v: (results.append(v + 1), threads.add(threading.get_ident())))
        qt.deliver(lb.fail.future("ValueError", "nope"), results.append, errors.append)
        spin(app, 3, lambda: len(frames) >= 3 and results and errors)
        watcher.stop()
    assert len(frames) >= 3 and results == [42] and isinstance(errors[0], ValueError)
    assert threads == {threading.get_ident()}


def test_viewer_picks_a_renderer_per_stream(tmp_path):
    app = qt.application()
    with insthub.Server("g", [sim.SimScope(), sim.SimCamera(), sim.SimSynth(), Multi("multi")], tcp=0) as server:
        remote = insthub.connect(server.addresses[1])
        window = viewer.Viewer(remote.scope, server.device("camera"), remote.multi, server.device("synth"))
        window.show()
        panels = {p.bus.name: p for p in window.panels}

        def ready():
            kinds = {(n, s): v.kind for n, p in panels.items() for s, v in p.views.items()}
            return (kinds.get(("scope", "ch1")) == "lines" and kinds.get(("camera", "image")) == "image"
                    and kinds.get(("multi", "note")) == "text" and kinds.get(("multi", "power")) == "lines"
                    and len(panels["multi"].views["traces"].curves) == 2 and panels["scope"].table.rowCount() == 3)

        spin(app, 8, ready)
        assert ready()
        assert panels["synth"].views["sweep"].kind == "text"            # declared, nothing published yet
        assert "connected" in panels["scope"].status.text()
        scope_view = panels["scope"].views["ch1"]
        scope_view.hover(scope_view.plot.vb.mapViewToScene(qt.QtCore.QPointF(0.004, 0.0)))
        assert "x = 0.004" in scope_view.readout
        window.centralWidget().setCurrentIndex(2)
        spin(app, 0.3)
        assert window.grab().save(str(tmp_path / "viewer.png"))
        window.close()
        spin(app, 0.2)
        assert server.devices["scope"].subscribers() == 0               # closing the window detached everything


def test_the_scope_panel_in_a_window_of_its_own(monkeypatch, capsys):
    """insthub's own panel (the reference for writing one), as `insthub panel` shows it."""
    from insthub import cli, sim_ui

    app = qt.application()
    with insthub.Server("g", [sim.SimScope()], tcp=0) as server:
        scope = server.devices["scope"]
        window = qt.device_window(sim_ui.scope, insthub.connect(server.addresses[1]).scope, "Scope panel")
        window.show()
        panel = window.centralWidget()
        assert window.windowTitle() == "Scope panel - scope"
        curve = panel.curves["ch1"]
        spin(app, 5, lambda: curve.getData()[0] is not None and panel.live.isChecked())
        t, volts = curve.getData()
        assert len(volts) == 1000 and abs(t[-1] - 0.01) < 1e-4            # 10 divisions of 1 ms, from its meta
        panel.timebase.setValue(2.0)                                      # ms/div: sent ...
        assert spin(app, 3, lambda: scope._timebase == 0.002)
        server.device("scope").set_timebase(0.005)                        # ... and shown when changed elsewhere
        assert spin(app, 3, lambda: panel.timebase.value() == 5.0)
        panel.measure()
        assert spin(app, 3, lambda: panel.result.text().startswith("Vpp "))
        panel.live.setChecked(False)
        assert spin(app, 3, lambda: not scope.tasks["_acquire"].enabled)
        window.close()                                                    # the panel closes with it ...
        assert spin(app, 3, lambda: scope.subscribers() == 0) and panel.watchers == []  # ... and watches nothing

        shown = []
        monkeypatch.setattr(qt, "run", lambda app=None: shown.extend(
            w for w in app_windows() if w.windowTitle() == "Quick - scope") or 0)
        assert cli.main(["-a", server.addresses[1], "panel", "scope", "insthub.sim_ui:scope", "--title", "Quick"]) == 0
        assert len(shown) == 1 and isinstance(shown[0].centralWidget(), sim_ui.ScopePanel)
        shown[0].close()
    assert cli.main(["panel", "scope", "nosuchpanel:make"]) == 1
    assert "error: cannot load the panel 'nosuchpanel:make': No module named 'nosuchpanel'" in capsys.readouterr().err


def app_windows():
    return [w for w in qt.QtWidgets.QApplication.topLevelWidgets() if w.isVisible()]


def test_a_stop_asked_before_the_event_loop_runs_is_not_lost():
    """A program that gets SIGTERM (as a launched panel does from Stop) while it is still starting ends."""
    import subprocess
    import sys

    code = ("import os, signal\nfrom insthub import qt\napp = qt.application()\n"
            "os.kill(os.getpid(), signal.SIGTERM)\nprint('ended', qt.run())\n")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30, check=False,
                          env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    assert done.stdout.strip() == "ended 0", done.stderr

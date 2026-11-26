"""The Qt helpers and the generic viewer, run without a display."""
import os
import time

import numpy as np
import pytest

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


def test_nothing_is_delivered_for_a_widget_that_is_gone():
    import gc
    from concurrent.futures import Future

    app = qt.application()
    window = qt.QtWidgets.QWidget()
    label, got = qt.QtWidgets.QLabel(window), []
    future = Future()
    qt.deliver(future, got.append, got.append, owner=label)
    del window
    gc.collect()                                                    # the label goes with its window ...
    future.set_result("late")                                       # ... before the answer comes
    spin(app, 0.2)
    assert got == []


def test_a_coalesced_or_followed_answer_reaches_no_widget_that_is_gone():
    import gc
    from concurrent.futures import Future

    app = qt.application()
    for make in (lambda call, done, error, owner: qt.coalesce(call, done, error, owner=owner),
                 lambda call, done, error, owner: qt.follow(call, done, error=error, owner=owner)):
        started, got = [], []

        def call(started=started, **values):
            started.append(Future())
            return started[-1]

        window = qt.QtWidgets.QWidget()
        label = qt.QtWidgets.QLabel(window)
        send = make(call, got.append, got.append, label)
        send(level=1)
        assert spin(app, 3, lambda started=started: started)            # (started off the GUI thread)
        del window, label
        gc.collect()                                                    # the panel is closed ...
        started[0].set_result("late")                                   # ... before the instrument answers
        spin(app, 0.2)
        assert got == []


class Connection:
    """What a proxy's calls are sent on, for start() to tell apart."""

    address = "test"


def test_start_does_not_wait_in_the_gui_thread_for_a_call_to_start():
    import threading
    from concurrent.futures import Future

    reconnected, started = threading.Event(), []

    class Proxy:
        _t = Connection()

        def future(self, n):                                        # its server went away: it reconnects first
            reconnected.wait(5)
            started.append(n)
            future = Future()
            future.set_result(n * 10)
            return future

    t = time.monotonic()
    futures = [qt.start(Proxy().future, n) for n in range(3)]
    assert time.monotonic() - t < 0.5 and not any(f.done() for f in futures)  # returned at once
    reconnected.set()
    assert [f.result(3) for f in futures] == [0, 10, 20] and started == [0, 1, 2]  # in the order made

    class Gone:
        _t = Connection()

        def future(self, n):
            time.sleep(0.3)
            raise insthub.ConnectionLost("cannot connect to tcp://nowhere:1")

    t = time.monotonic()
    futures = [qt.start(Gone().future, n) for n in range(4)]
    for f in futures:
        with pytest.raises(insthub.ConnectionLost):
            f.result(3)
    assert time.monotonic() - t < 0.9                               # those behind the first fail with it


class Calls:
    """What a coalesced sender started: each call's values, and its future, for the test to end."""

    def __init__(self):
        self.started = []

    def __call__(self, **values):
        from concurrent.futures import Future

        future = Future()
        self.started.append((values, future))
        return future

    def end(self, i, result=None, error=None):
        future = self.started[i][1]
        future.set_exception(error) if error else future.set_result(result)


def test_coalesce_keeps_one_call_on_its_way_with_the_newest_values():
    app = qt.application()
    calls, done = Calls(), []
    send = qt.coalesce(calls, done.append)
    send(level=1)
    send(level=2)                                                   # while the first is on its way: they wait ...
    send(gain=5)
    send(level=3)
    assert spin(app, 3, lambda: calls.started) and send.busy
    assert [v for v, _ in calls.started] == [{"level": 1}]
    calls.end(0, "one")
    assert spin(app, 3, lambda: len(calls.started) == 2)
    assert calls.started[1][0] == {"level": 3, "gain": 5} and done == []  # ... merged: the newest of each key
    calls.end(1, "three")                                           # nothing waits behind it: its result is shown
    assert spin(app, 3, lambda: done == ["three"]) and not send.busy
    send(level=4)                                                   # idle: it goes out at once
    assert spin(app, 3, lambda: len(calls.started) == 3) and calls.started[2][0] == {"level": 4}


def test_coalesce_merges_keys_that_go_together_as_it_is_told():
    app = qt.application()
    calls = Calls()

    def merge(waiting, new):  # a center (or a span) replaces a start and a stop
        if new.keys() & {"center", "span"}:
            waiting = {k: v for k, v in waiting.items() if k not in ("start", "stop")}
        return {**waiting, **new}

    send = qt.coalesce(calls, merge=merge)
    send(start=1, stop=2)
    send(start=3, stop=4)
    send(center=5)
    send(span=1)
    assert spin(app, 3, lambda: calls.started)
    calls.end(0)
    assert spin(app, 3, lambda: len(calls.started) == 2) and calls.started[1][0] == {"center": 5, "span": 1}


def test_coalesce_says_what_failed_and_goes_on():
    app = qt.application()
    calls, done, errors, busy = Calls(), [], [], []
    send = qt.coalesce(calls, done.append, lambda e: (errors.append(e), busy.append(send.busy)))
    send(level=1)
    send(level=2)
    assert spin(app, 3, lambda: calls.started)
    calls.end(0, error=ValueError("too high"))                      # said, and what waits still goes out
    assert spin(app, 3, lambda: len(calls.started) == 2) and [str(e) for e in errors] == ["too high"]
    assert busy == [True]                                           # (it had gone out already: busy says so)
    calls.end(1, "two")
    assert spin(app, 3, lambda: done == ["two"])

    def gone(**values):
        time.sleep(0.2)                                             # (trying to reach the server again)
        raise insthub.ConnectionLost("cannot connect to tcp://nowhere:1")
    send = qt.coalesce(gone, done.append, lambda e: (errors.append(e), busy.append(send.busy)))
    send(level=3)                                                   # a call that cannot even start fails too ...
    assert send.busy                                                # (and what is sent meanwhile waits)
    assert spin(app, 3, lambda: len(errors) == 2) and isinstance(errors[-1], insthub.ConnectionLost)
    assert not send.busy and busy[-1] is False
    send(level=4)                                                   # ... and leaves nothing stuck
    assert spin(app, 3, lambda: len(errors) == 3) and not send.busy


class Slow(insthub.Device):
    """A setting the instrument takes slowly."""

    def open(self):
        self.taken = []

    @insthub.command
    def set_level(self, level: float) -> float:
        time.sleep(0.05)
        self.taken.append(level)
        return level


def test_coalesce_spares_a_slow_instrument_the_backlog():
    app = qt.application()
    with insthub.Server("g", [Slow("slow")], tcp=0) as server:
        slow, done = insthub.connect(server.addresses[-1]).slow, []
        send = qt.coalesce(slow.set_level.future, done.append)
        for level in range(20):                                     # a slider dragged across its range
            send(level=level)
            spin(app, 0.01)
        assert spin(app, 5, lambda: done == [19])
        taken = server.devices["slow"].taken
        assert taken[0] == 0 and taken[-1] == 19 and len(taken) < 10   # the first, the last, a few between


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
        panel.timebase.setValue(0.002)                                    # s/div: sent ...
        assert spin(app, 3, lambda: scope._timebase == 0.002)
        server.device("scope").set_timebase(0.005)                        # ... and shown when changed elsewhere
        assert spin(app, 3, lambda: panel.timebase.value() == 0.005)
        assert [float(x) for x in panel.timebase.opts["bounds"]] == [1e-9, 10]  # the device's own limits (describe())
        server.device("scope").set_timebase(2.5e-8)                       # all of them: 25 ns is shown as it is
        assert spin(app, 3, lambda: panel.timebase.text() == "25 ns/div")
        panel.timebase.setValue(100)                                      # beyond them: the most it takes
        assert spin(app, 3, lambda: scope._timebase == 10)
        panel.measure()
        assert spin(app, 3, lambda: panel.result.text().startswith("Vpp "))
        commands = panel.controls.commands                                # the rest: the generic controls
        assert commands.shown == ["get_waveform", "identify", "measure", "set_signal"]
        commands.choose("set_signal")
        commands.args.setText("2500")
        commands.run()
        assert spin(app, 3, lambda: scope._signal_hz == 2500)
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


class SteppedScope(sim.SimScope):
    """A scope that takes only 1-2-5 steps up to 1 s/div, and settles after saying which (as a real one may)."""

    @insthub.command(params={"seconds_per_div": {"unit": "s", "min": 1e-9, "max": 10}})
    def set_timebase(self, seconds_per_div: float) -> float:
        if seconds_per_div > 1:
            raise ValueError("this one stops at 1 s/div")
        self._timebase = 0.001 if seconds_per_div < 0.0015 else 0.002
        self.set_state(timebase=self._timebase)                     # reported while the call is still on its way
        time.sleep(0.5)
        return self._timebase


def test_the_scope_panel_shows_the_timebase_the_scope_took():
    """Not the one typed, when the scope rounds it or refuses it: the panel never disagrees with the device."""
    from insthub import sim_ui

    app = qt.application()
    with insthub.Server("g", [SteppedScope("scope")], tcp=0) as server:
        scope = server.devices["scope"]
        panel = sim_ui.scope(insthub.connect(server.addresses[-1]).scope)
        assert spin(app, 3, lambda: panel.reported.get("timebase") == 0.001)
        panel.timebase.setValue(0.0018)                             # rounded to 2 ms
        assert spin(app, 3, lambda: scope._timebase == 0.002 and not panel.set_timebase.busy)
        assert spin(app, 1, lambda: panel.timebase.value() == 0.002)
        panel.timebase.setValue(5)                                  # refused
        assert spin(app, 3, lambda: panel.result.text() == "failed: this one stops at 1 s/div")
        assert panel.timebase.value() == 0.002 and scope._timebase == 0.002
        panel.result.clear()
        panel.timebase.setValue(0.0012)                             # 1 ms, said while it settles ...
        assert spin(app, 3, lambda: scope._timebase == 0.001) and panel.set_timebase.busy
        panel.timebase.setValue(5)                                  # ... when one it refuses goes out after it
        assert spin(app, 3, lambda: panel.result.text() == "failed: this one stops at 1 s/div")
        assert panel.timebase.value() == 0.001                      # what it said meanwhile was kept
        panel.close()


def test_a_stop_asked_before_the_event_loop_runs_is_not_lost():
    """A program that gets SIGTERM (as a launched panel does from Stop) while it is still starting ends."""
    import subprocess
    import sys

    code = ("import os, signal\nfrom insthub import qt\napp = qt.application()\n"
            "os.kill(os.getpid(), signal.SIGTERM)\nprint('ended', qt.run())\n")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30, check=False,
                          env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    assert done.stdout.strip() == "ended 0", done.stderr

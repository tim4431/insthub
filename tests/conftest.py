import logging
import os
import subprocess
import sys
import threading
import time

import pytest

import insthub
from insthub import registry, sim

# Before any test module imports Qt (this file is read first): no window on a real screen, and none needed
# (a test module run alone, without a display, would abort the run).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _isolated_bus(tmp_path_factory, monkeypatch):
    """Each test gets its own runtime directory: no interference with real servers.  Of the
    extensions, only insthub's own: not those this computer's user, services and servers have."""
    path = tmp_path_factory.mktemp("bus")
    monkeypatch.setenv("INSTHUB_DIR", str(path))
    monkeypatch.setenv("INSTHUB_DEVICES", str(path / "devices.toml"))
    monkeypatch.setenv("INSTHUB_EXTENSIONS", "none")
    insthub.client._remote_seen[0] = float("-inf")  # what servers on other computers said: asked again
    yield
    insthub.close_all()
    for server in insthub.server.local_servers():
        server.stop()


@pytest.fixture(autouse=True)
def _process_wide_logging(monkeypatch):
    """What a test that runs `insthub serve` or `insthub sim` in this process changes for the
    whole process (server.keep_logs(everything=True)) is put back: the hooks for exceptions that
    nothing caught (PyQt hands an exception raised in a slot to a hook that is not Python's own,
    instead of failing the test), and where the log is kept from.  (Its basicConfig() does nothing
    here: pytest's own handlers are on the root logger.)"""
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    root, ours, recent = logging.getLogger(), logging.getLogger("insthub"), insthub.server._RECENT
    on_root, ours_before, level = recent in root.handlers, list(ours.handlers), root.level
    yield
    if not on_root:
        root.removeHandler(recent)
    for handler in [h for h in ours.handlers if h not in ours_before]:
        ours.removeHandler(handler)
    for handler in ours_before:
        ours.addHandler(handler)  # (no-op for one still there)
    root.setLevel(level)


@pytest.fixture
def server():
    with insthub.Server("t", [*sim.devices(), sim.Loopback()], tcp=0) as srv:
        yield srv


@pytest.fixture(params=["inproc", "ipc", "tcp"])
def client(request, server):
    address = {"inproc": "inproc://t", "ipc": server.addresses[0], "tcp": server.addresses[1]}[request.param]
    with insthub.connect(address) as c:
        c.kind = request.param
        yield c


class Spawned:
    """A server in another process (`python -m insthub sim`)."""

    def __init__(self, name="child", *extra):
        self.name, self.extra, self.proc = name, extra, None

    def start(self):
        self.proc = subprocess.Popen([sys.executable, "-m", "insthub", "sim", "--name", self.name, *self.extra],
                                     env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        end = time.monotonic() + 10
        while time.monotonic() < end:
            if any(s["server"] == self.name for s in registry.servers()):
                return self
            if self.proc.poll() is not None:
                raise RuntimeError(self.proc.stderr.read().decode())
            time.sleep(0.02)
        raise TimeoutError("server did not come up")

    def stop(self, kill=False):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill() if kill else self.proc.terminate()
            self.proc.wait(10)
        if self.proc is not None and self.proc.stderr:
            self.proc.stderr.close()
        self.proc = None


@pytest.fixture
def spawned():
    child = Spawned()
    yield child
    child.stop()

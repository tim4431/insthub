import os
import subprocess
import sys
import time

import pytest

import insthub
from insthub import registry, sim


@pytest.fixture(autouse=True)
def _isolated_bus(tmp_path_factory, monkeypatch):
    """Each test gets its own runtime directory: no interference with real servers."""
    path = tmp_path_factory.mktemp("bus")
    monkeypatch.setenv("INSTHUB_DIR", str(path))
    monkeypatch.setenv("INSTHUB_DEVICES", str(path / "devices.toml"))
    yield
    insthub.close_all()
    for server in insthub.server.local_servers():
        server.stop()


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

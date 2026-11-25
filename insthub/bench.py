"""`insthub bench`: call latency and data throughput on this machine.

The in-process numbers use a server in this process; the ipc and tcp numbers
use a server in a child process, which is how instruments are normally hosted.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

import numpy as np

from . import Server, connect, gather, registry, sim


def _latency(fn, n: int) -> tuple[float, float]:
    """(median, 99th percentile) duration of fn() in microseconds."""
    times = np.empty(n)
    perf = time.perf_counter
    for i in range(n):
        start = perf()
        fn()
        times[i] = perf() - start
    return float(np.median(times) * 1e6), float(np.percentile(times, 99) * 1e6)


def _measure(label: str, client, quick: bool) -> list[tuple[str, str]]:
    lb = client.loopback
    by_reference = client.address.startswith("inproc")
    n = 2000 if quick else 20000
    rows = []
    lb.echo(0)
    med, p99 = _latency(lb.echo_now, n)
    rows.append(("call, answered at once (concurrent)", f"{med:7.1f} us   (p99 {p99:.0f} us)"))
    med, p99 = _latency(lb.echo, n)
    rows.append(("call through the device thread", f"{med:7.1f} us   (p99 {p99:.0f} us)"))

    start = time.perf_counter()
    gather(*[lb.echo.future(i) for i in range(n)])
    rows.append(("pipelined calls (futures)", f"{n / (time.perf_counter() - start):9.0f} calls/s"))
    start = time.perf_counter()
    for i in range(n):
        lb.echo.notify(i)
    lb.echo(0)
    rows.append(("notify (no reply)", f"{n / (time.perf_counter() - start):9.0f} calls/s"))

    for nbytes, reps in ((4_000, 2000), (1_000_000, 200), (100_000_000, 5)):
        if quick:
            reps = max(2, reps // 10)
        data = np.zeros(nbytes, np.uint8)
        lb.echo(data)
        start = time.perf_counter()
        for _ in range(reps):
            lb.echo(data)
        each = (time.perf_counter() - start) / reps
        size = f"{nbytes / 1e6:g} MB" if nbytes >= 1e6 else f"{nbytes / 1e3:g} kB"
        rate = "passed by reference" if by_reference else f"{2 * nbytes / each / 1e6:7.0f} MB/s"
        rows.append((f"array there and back, {size}", f"{each * 1e6:9.0f} us   ({rate})"))

    count = 2000 if quick else 20000
    with lb.subscribe("small", queue=count) as sub:
        start = time.perf_counter()
        lb.flood.notify("small", count, 4000)
        for _ in range(count):
            sub.get(30)
        rows.append(("stream, 4 kB frames (1000-point trace)", f"{count / (time.perf_counter() - start):9.0f} frames/s"))
    count, nbytes = (10, 8_000_000) if quick else (60, 8_000_000)
    with lb.subscribe("large", queue=count) as sub:
        start = time.perf_counter()
        lb.flood.notify("large", count, nbytes)
        for _ in range(count):
            sub.get(30)
        took = time.perf_counter() - start
        rate = "passed by reference" if by_reference else f"{count * nbytes / took / 1e6:.0f} MB/s"
        rows.append(("stream, 8 MB frames (camera image)", f"{count / took:9.0f} frames/s  ({rate})"))
    return [(f"{label}: {name}", value) for name, value in rows]


def main(address: str | None = None, quick: bool = False) -> int:
    logging.getLogger("insthub").setLevel(logging.WARNING)
    rows: list[tuple[str, str]] = []
    child = None
    try:
        if address:
            with connect(address) as client:
                rows += _measure(address.split("://")[0], client, quick)
        else:
            with Server(f"bench-{os.getpid()}", [sim.Loopback()], ipc=False) as server:
                with connect(f"inproc://{server.name}") as client:
                    rows += _measure("in-process", client, quick)
            name = f"bench-child-{os.getpid()}"
            child = subprocess.Popen([sys.executable, "-m", "insthub", "sim", "--name", name, "--tcp", "0"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            entry, deadline = None, time.monotonic() + 15
            while entry is None and time.monotonic() < deadline:
                entry = next((s for s in registry.servers() if s["server"] == name), None)
                time.sleep(0.02)
            if entry is None:
                print("could not start the benchmark server", file=sys.stderr)
                return 1
            for addr in entry["addresses"]:
                kind = "unix socket" if addr.startswith("ipc") else "tcp loopback"
                with connect(addr) as client:
                    rows += _measure(kind, client, quick)
    finally:
        if child is not None:
            child.terminate()
            child.wait(10)
    width = max(len(name) for name, _ in rows)
    last = None
    for name, value in rows:
        group = name.split(":")[0]
        if group != last:
            print()
            last = group
        print(f"{name:{width}s}  {value}")
    return 0

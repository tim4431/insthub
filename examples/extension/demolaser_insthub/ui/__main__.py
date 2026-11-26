"""The demo laser's app: a whole program, for a device whose window is more than a panel
(`[ui.app]` in insthub-extension.toml: `insthub open laser app`).

    python -m demolaser_insthub.ui --address ADDRESS --name NAME   # the laser NAME of the server there
    python -m demolaser_insthub.ui [--name NAME]                   # the laser NAME, or one of its own

Given an address, as insthub gives it when it starts the app for a device, the app only attaches
to that laser, and fails if it is not there.  Without one, it uses the laser of that name wherever
it runs, and hosts one itself only if none runs: then scripts still reach the laser
(`insthub.device("laser")`) while the app is open.  An address must mean "only attach": an app that
hosted a laser whenever the name did not resolve at that moment would start a second server for
the same instrument while its own server restarts.  The app imports the device (and with it the
driver) only to host one.  This one shows the panel; a real app shows more.
"""
from __future__ import annotations

import argparse
import sys

import insthub
from insthub import qt

from .. import EXTENSION  # what its lasers announce (describe()["extension"]): how it knows one of its own
from .panel import make


def connect(name: str, address: str | None = None) -> tuple:
    """(a proxy of the laser NAME, and the server this program hosts it in, or None).  Raises BusError
    if there is no device of that name at `address`, RuntimeError if the one there is no demo laser,
    ValueError if `address` is not an address."""
    if address is None:
        try:
            address = insthub.locate(name)
        except insthub.DeviceNotFound:
            from ..device import Laser  # (only here: hosting one needs the driver)

            server = insthub.Server(name, [Laser(name)]).start()
            return server.device(name), server
    device = insthub.device(name, address)
    found = device.bus.describe().get("extension")
    if found != EXTENSION:
        raise RuntimeError(f"{name} at {address} is not a demo laser (its extension: {found or 'none'})")
    return device, None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m demolaser_insthub.ui",
                                     description="The demo laser's app (insthub open laser app).")
    parser.add_argument("--address", help="only attach to the laser of the server there; never host one")
    parser.add_argument("--name", default="laser", help="the laser's device name (default: laser)")
    args = parser.parse_args(argv)
    try:
        device, server = connect(args.name, args.address)
    except (insthub.BusError, RuntimeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    window = qt.device_window(make, device, "Demo laser")
    window.statusBar().showMessage(f"{args.name} is hosted by this program: scripts reach it while it is open"
                                   if server else f"{args.name} at {device.bus.address}")
    window.show()
    try:
        return qt.run()
    finally:
        if server is not None:
            server.stop()


if __name__ == "__main__":
    sys.exit(main())

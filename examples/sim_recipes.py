"""Recipes for the simulated instruments of `insthub sim` (see docs/guide.md#recipes).

    insthub sim                                           # terminal 1
    insthub run examples/sim_recipes.py:cw mhz=2500       # the synth's output on, at 2500 MHz
    insthub run examples/sim_recipes.py:tone_scan --dry-run
    insthub run examples/sim_recipes.py:tone_scan stop=1050 steps=6
    insthub run examples/sim_recipes.py:off

Each public function of this file is a recipe. Copy the file into ~/.config/insthub/recipes/ and
they have names (`insthub run cw`, or `insthub run sim_recipes.cw`), and insthub gui shows each
one on the pages of the devices it uses.
"""
from typing import Literal

import numpy as np

import insthub
from insthub.recipes import recipe


def cw(mhz: float = 1000, dbm: float = 0):
    """The synth's output on, at one frequency and power."""
    synth = insthub.device("synth")
    synth.frequency = mhz
    synth.power = dbm
    synth.rf(True)


def off():
    """The synth's output off."""
    insthub.device("synth").rf(False)


@recipe(params={"start": {"unit": "MHz", "min": 10, "max": 24000}, "stop": {"unit": "MHz", "min": 10, "max": 24000},
                "steps": {"min": 2}, "dbm": {"unit": "dBm", "doc": "the synth's power"}})
def tone_scan(start: float = 1000, stop: float = 1100, steps: int = 11, dbm: float = 0):
    """Step the synth, and read the tone on the analyzer at each step.

    The analyzer follows the synth and takes the level of its highest point, which is the
    synth's power less the 20 dB of the attenuator between them.  Returns the frequencies
    (MHz) and the levels (dBm), as arrays.  The synth's output is off again at the end, also
    when the scan fails or is stopped."""
    synth = insthub.device("synth")  # named in the function: only the recipes that use a device need it
    sa = insthub.device("sa")
    sa.span = 1e6
    synth.power = dbm
    synth.rf(True)
    mhz = np.linspace(start, stop, steps)
    dbm_seen = np.empty(steps)
    try:
        for i, f in enumerate(mhz):
            synth.frequency = f
            sa.center = f * 1e6
            dbm_seen[i] = sa.peak()["level_dBm"]
            print(f"{f:8.1f} MHz  {dbm_seen[i]:6.1f} dBm")  # shown as it runs, in the window too
    finally:
        synth.rf(False)
    return {"mhz": mhz, "dbm": dbm_seen}


def scope_reading(item: Literal["VPP", "VRMS", "FREQ"] = "VPP", channel: Literal[1, 2] = 1) -> float:
    """One measurement of the scope (the choices come from the annotations)."""
    return insthub.device("scope").measure(item, channel=channel)

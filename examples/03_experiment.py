"""An experiment loop driving several instruments at once.

    insthub sim                      # terminal 1: simulated scope, synth and camera
    python examples/03_experiment.py

The loop steps a synthesizer and, at every step, reads the scope and the camera
*in parallel* (each device has its own thread on the server), while a recorder
keeps every live scope trace in the background.
"""
import time

import numpy as np

import insthub

synth, scope, camera = (insthub.device(name) for name in ("synth", "scope", "camera"))

recorded = []
recorder = scope.subscribe("ch1", recorded.append, queue=1000)       # every frame, in order, on its own thread

synth.rf(True)
results = []
start = time.perf_counter()
for freq in np.linspace(1000, 2000, 21):
    synth.frequency = float(freq)                                     # set, and wait until it is done
    waveform, image, vpp = insthub.gather(                             # three readouts at the same time
        scope.get_waveform.future(1),
        camera.grab.future(),
        scope.measure.future("VPP", channel=2),
        timeout=10)
    results.append((round(float(freq)), round(float(np.ptp(waveform.volts)), 3), int(image.max()), round(vpp, 3)))
elapsed = time.perf_counter() - start
synth.rf.notify(False)                                                # fire and forget

recorder.close()
print(f"{len(results)} steps in {elapsed * 1e3:.0f} ms ({elapsed / len(results) * 1e3:.1f} ms per step)")
print("first / last:", results[0], results[-1])
print(f"recorded {len(recorded)} live traces meanwhile (skipped {recorder.dropped})")

"""Using a device from an experiment script.  Start examples/01_server.py first."""
import insthub

lockin = insthub.device("lockin")                 # found by name; no address needed on the same machine
print(lockin.bus.help())

# Blocking calls and properties read like a local driver object.
lockin.set_time_constant(0.3)
lockin.frequency = 1234.5
print("frequency:", lockin.frequency, " state:", lockin.state())
trace = lockin.read(samples=5000)
print("trace:", trace.shape, trace.dtype, "mean %.4f" % trace.mean())

# Errors arrive as the exception the device raised.
try:
    lockin.set_time_constant(-1)
except ValueError as e:
    print("refused:", e)

# Asynchronous: start several things, collect them later.
futures = [lockin.read.future(1000) for _ in range(5)]
print("5 reads:", [round(float(r.mean()), 3) for r in insthub.gather(*futures, timeout=10)])

# Streams: data the server publishes by itself.
with lockin.subscribe("xy") as sub:
    for _ in range(3):
        frame = sub.get(timeout=2)
        print(f"xy #{frame.seq}: x={frame.data['x']:.4f}  y={frame.data['y']:.4f}  ({frame.age * 1e3:.2f} ms old)")

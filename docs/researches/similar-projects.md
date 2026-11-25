# Open-source lab control projects similar to insthub

*The comparison the owner started from (November 2026). The notes in this folder study these projects in depth.*

**nspyre is the closest overall match. PyLabNet is the closest match for a multi-computer quantum optics lab.**

[insthub](https://github.com/tim4431/insthub) lets one process own each instrument connection while scripts, notebooks, and GUIs share access. A driver can publish live traces or camera frames directly to subscribers. Each device has its own command queue, and servers can run independently. [Architecture](https://github.com/tim4431/insthub/blob/main/docs/architecture.md)

## Comparable projects

| Project | How it compares with insthub | Source |
| --- | --- | --- |
| **nspyre** | Developed by the University of Chicago's Awschalom Group. Its `InstrumentServer` hosts existing Python drivers and exposes them over RPyC. It also provides Qt tools and live plots. It uses a separate `DataServer`, with `DataSource` and `DataSink` for publishing and receiving datasets. In insthub, clients subscribe directly to a device's streams. | [GitHub](https://github.com/nspyre-org/nspyre) · [Instrument server](https://nspyre.readthedocs.io/en/latest/instrument_server.html) · [Data server](https://nspyre.readthedocs.io/en/latest/data_server.html) |
| **PyLabNet** | The Lukin Group at Harvard built this Python client/server system for solid-state quantum optics and quantum networking. Its Launch Control GUI, remote device servers, and centralized logging show how a lab can operate instruments distributed across several computers. | [GitHub](https://github.com/lukingroup/pylabnet) |
| **InstrumentServer** | A close match for insthub's Python proxy. The server owns the hardware connection, and the proxy preserves the parameters, methods, and submodules of the underlying QCoDeS driver. | [GitHub](https://github.com/toolsforexperiments/instrumentserver) · [Python client](https://toolsforexperiments.github.io/instrumentserver/user_guide/client.html) |
| **PyroLab** | BYU CamachoLab uses Pyro5 to expose remote instruments as Python objects. A driver that needs a Windows-only Thorlabs DLL can stay on the acquisition PC and be called from Linux. A nameserver handles registration and discovery. | [GitHub](https://github.com/BYUCamachoLab/pyrolab) · [Nameservers](https://pyrolab.readthedocs.io/en/stable/user_guide/nameservers.html) |
| **LabRAD** | An asynchronous RPC framework for scientific labs. Clients and servers connect through a central Manager. In insthub, local discovery uses registration files and clients connect to each server directly. | [Python client](https://github.com/labrad/pylabrad) · [Manager](https://github.com/labrad/scalabrad) |
| **Galois Edge** | The closest match for insthub's MCP integration. Its daemon exposes SCPI and other devices through MCP and gRPC, using protocol adapters and instrument profiles. insthub can expose methods and properties from an existing Python driver. | [GitHub](https://github.com/Galois-Labs/edge) · [insthub MCP docs](https://github.com/tim4431/insthub/blob/main/docs/agents.md) |

The [nspyre experiment template](https://github.com/nspyre-org/template) is the best starting point for a runnable example of remote instrument access and live data. [PyLabNet's Launch Control](https://github.com/lukingroup/pylabnet#usage) is the better reference for operating device servers across multiple lab computers.

## Precisely timed experimental shots

If the experiment needs DDS, TTL, DAC, and camera triggers to run on a shared timeline within each shot, these projects cover that part of the control system.

- **labscript suite** uses BLACS to queue shots and load instructions onto devices ahead of hardware-timed execution. [GitHub](https://github.com/labscript-suite) · [BLACS documentation](https://docs.labscriptsuite.org/projects/blacs/en/latest/)
- **ARTIQ** compiles timing-sensitive experiment code for execution on dedicated hardware with nanosecond timing resolution. [GitHub](https://github.com/m-labs/artiq) · [ARTIQ introduction](https://m-labs.hk/artiq/manual/introduction.html)

In an optics experiment, insthub can configure instruments and stream live data while the timing hardware executes triggers within each shot.

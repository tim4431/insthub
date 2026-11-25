"""insthub: a fast command and data bus between instrument servers, GUIs and
experiment scripts.

Server side (owns the hardware):

    import insthub

    class Synth(insthub.Device):
        def open(self): ...                         # connect to the instrument
        @insthub.command
        def set_frequency(self, mhz: float): ...
        @insthub.periodic(1.0)
        def _poll(self): self.publish("temperature", self.read_temperature())

    insthub.serve(Synth("synth"))                    # or insthub.wrap(ExistingDriver, "name")

Client side (experiment script, notebook, GUI):

    synth = insthub.device("synth")
    synth.set_frequency(2500.0)                     # blocking
    fut = synth.set_frequency.future(2500.0)        # asynchronous
    for frame in synth.subscribe("temperature"): ...

See README.md.
"""
from .client import (
                     CallFuture,
                     Client,
                     DeviceProxy,
                     Subscription,
                     close_all,
                     connect,
                     device,
                     discover,
                     gather,
                     locate,
)
from .codec import Record, register_type
from .device import Device, Frame, Task, Wrapped, command, periodic, prop, wrap
from .errors import (
                     BusError,
                     ConnectionLost,
                     DeviceNotConnected,
                     DeviceNotFound,
                     RemoteError,
                     SubscriptionClosed,
                     UnknownCommand,
)
from .server import Server, serve

__version__ = "0.1.0"

__all__ = [
    "BusError", "CallFuture", "Client", "ConnectionLost", "Device", "DeviceNotConnected", "DeviceNotFound",
    "DeviceProxy", "Frame", "Record", "RemoteError", "Server", "Subscription", "SubscriptionClosed", "Task",
    "UnknownCommand", "Wrapped", "close_all", "command", "connect", "device", "discover", "gather", "locate", "periodic",
    "prop", "register_type", "serve", "wrap",
]

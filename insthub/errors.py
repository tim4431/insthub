"""Exceptions.

An exception raised by a device command travels to the caller as text (class
names, message, traceback) and is raised there as a `RemoteError`.  When the
original class derives from a common builtin (`ValueError`, `TimeoutError`,
`RuntimeError`, ...), the raised object is *also* an instance of that builtin,
so `except ValueError:` works the same for a local driver and a remote one.
"""
from __future__ import annotations

import traceback


class BusError(Exception):
    """Base class of everything insthub itself raises."""


class ConnectionLost(BusError, ConnectionError):
    """The server cannot be reached, or went away before it replied.

    A call that was already sent may or may not have run."""


class DeviceNotFound(BusError, LookupError):
    """No device of that name (on this server, or anywhere on the bus)."""


class UnknownCommand(BusError, AttributeError):
    """The device has no such command, property, parameter or stream."""


class DeviceNotConnected(BusError):
    """The server is up, but its hardware session could not be opened."""


class DevicePaused(DeviceNotConnected):
    """Somebody paused the device (`dev.bus.pause()`, `insthub pause`, the window's Pause):
    none of its commands, properties or periodic tasks run until it is resumed."""


class SubscriptionClosed(BusError):
    """The subscription was closed while waiting for a frame."""


class RemoteError(BusError):
    """An exception raised on the server side while running a command.

    Attributes: `remote_type` (original class name), `remote_bases` (its base
    class names), `remote_traceback` (formatted text) and `where`
    ("device.command").  `str(e)` is the original message."""

    remote_type = ""
    remote_bases: tuple = ()
    remote_traceback = ""
    where = ""

    def __str__(self) -> str:
        return self.args[0] if self.args else ""


# Builtins a remote exception may be re-raised as.  Order matters only for
# readability: the remote MRO is walked most-derived first.
_BUILTINS = {
    cls.__name__: cls
    for cls in (
        ValueError, TypeError, KeyError, IndexError, LookupError, AttributeError,
        RuntimeError, NotImplementedError, TimeoutError, ConnectionError, PermissionError,
        FileNotFoundError, OSError, ZeroDivisionError, OverflowError, ArithmeticError,
        AssertionError, EOFError,
    )
}
_OWN = {cls.__name__: cls for cls in (DeviceNotFound, UnknownCommand, DevicePaused, DeviceNotConnected,
                                       ConnectionLost)}
_remote_classes: dict[type, type] = {}


def did_you_mean(word: str, names) -> str:
    """The end of a message about a misspelled name: "; did you mean 'port'?", or "" if no
    name is close."""
    import difflib  # here, not at the top: only a mistake needs it

    close = difflib.get_close_matches(word, list(names), n=1)
    return f"; did you mean {close[0]!r}?" if close else ""


def describe_exception(exc: BaseException) -> list:
    """What goes on the wire: [class names along the MRO, message, traceback text]."""
    names = [c.__name__ for c in type(exc).__mro__ if c not in (object, BaseException, Exception)]
    if isinstance(exc, RemoteError) and exc.remote_type:  # relayed from a further hop: keep the origin
        names = [exc.remote_type, *exc.remote_bases]
        tb = exc.remote_traceback
    else:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return [names, str(exc), tb]


def build_exception(names: list, message: str, tb: str, where: str = "") -> BaseException:
    """Inverse of `describe_exception`: the exception to raise on the calling side."""
    for name in names:  # insthub's own errors come back as themselves
        cls = _OWN.get(name)
        if cls is not None:
            return cls(message)
    base = next((_BUILTINS[n] for n in names if n in _BUILTINS), None)
    if base is None:
        cls = RemoteError
    else:
        cls = _remote_classes.get(base)
        if cls is None:
            cls = _remote_classes[base] = type(f"Remote{base.__name__}", (RemoteError, base), {})
    exc = cls(message)
    exc.remote_type = names[0] if names else "Exception"
    exc.remote_bases = tuple(names[1:])
    exc.remote_traceback = tb
    exc.where = where
    if tb:
        exc.add_note(f"--- remote traceback ({where or 'server'}) ---\n{tb.rstrip()}")
    return exc

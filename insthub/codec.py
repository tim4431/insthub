"""Wire codec: MessagePack for structure, raw out-of-band buffers for bulk data.

`encode(obj)` returns `(body, buffers)`: a small MessagePack blob plus a list of
memoryviews onto the original numpy arrays / byte strings.  The transport sends
the buffers straight from that memory and the receiver reads each one straight
into the array it becomes, so bulk data is never serialized, base64'd or copied
in Python.

What can travel (anything else raises `TypeError` on the sending side):

    None, bool, int, float, complex, str, bytes/bytearray
    list, tuple, dict, set, frozenset           (tuples stay tuples)
    numpy arrays of any non-object dtype, numpy scalars (arrive as Python scalars)
    Enum members (arrive as their value), pathlib paths (arrive as str)
    dataclass instances and classes registered with `register_type`

No pickle: decoding never runs code chosen by the peer.  A dataclass is rebuilt
only if its class is registered or its module is already imported here;
otherwise it arrives as a `Record` (a dict with attribute access).
"""
from __future__ import annotations

import dataclasses
import enum
import struct
import sys
import threading
from pathlib import PurePath

import msgpack
import numpy as np
from msgpack import ExtType

#: Arrays and byte strings up to this size travel inside the message body;
#: larger ones go out of band (zero-copy).
INLINE_MAX = 2048
#: Containers longer than this are not searched for large byte strings to lift.
_LIFT_SCAN_MAX = 64

_T_TUPLE, _T_ARRAY, _T_BLOB, _T_COMPLEX, _T_SET, _T_FROZENSET, _T_STRUCT = range(1, 8)
_COMPLEX = struct.Struct("<dd")
_U32 = struct.Struct("<I")


class Record(dict):
    """A structured value whose class is not known on this side: a dict whose
    keys are also attributes.  `type_name` is the sender's class name."""

    type_name = ""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None

    def __repr__(self) -> str:
        return f"Record<{self.type_name}>({dict.__repr__(self)})"


class _Oob:
    """Marks a byte string to be sent out of band."""

    __slots__ = ("data",)

    def __init__(self, data):
        self.data = data


# ---------------------------------------------------------------- type registry
_by_type: dict[type, tuple] = {}  # class -> (wire name, to_dict)
_by_name: dict[str, object] = {}  # wire name -> from_dict
_registry_lock = threading.Lock()


def register_type(cls=None, *, name: str | None = None, encode=None, decode=None):
    """Make a class sendable.  Usable as a decorator.

    Dataclasses need no arguments.  For any other class give `encode(obj) -> dict`
    and `decode(dict) -> obj`.  `name` is the name on the wire (default:
    "module.QualName"); both ends must use the same one."""

    def register(c):
        wire_name = name or f"{c.__module__}.{c.__qualname__}"
        to_dict, from_dict = encode, decode
        if to_dict is None or from_dict is None:
            if not dataclasses.is_dataclass(c):
                raise TypeError(f"{c.__name__} is not a dataclass: pass encode= and decode=")
            names = tuple(f.name for f in dataclasses.fields(c))
            to_dict = to_dict or (lambda o, _n=names: {k: getattr(o, k) for k in _n})
            from_dict = from_dict or (lambda d, _c=c: _c(**d))
        with _registry_lock:
            _by_type[c] = (wire_name, to_dict)
            _by_name[wire_name] = from_dict
        return c

    return register(cls) if cls is not None else register


def _find_loaded_dataclass(name: str):
    """A dataclass called `name` ("pkg.mod.Class") in an *already imported*
    module, registered on the fly.  Nothing is ever imported by wire name."""
    pos = len(name)
    while (pos := name.rfind(".", 0, pos)) > 0:
        obj = sys.modules.get(name[:pos])
        if obj is None:
            continue
        for part in name[pos + 1:].split("."):
            obj = getattr(obj, part, None)
        if isinstance(obj, type) and dataclasses.is_dataclass(obj):
            register_type(obj, name=name)
            return _by_name[name]
        return None
    return None


# ---------------------------------------------------------------- encoding
def _dtype_descr(dt: np.dtype):
    if dt.hasobject:
        raise TypeError("numpy arrays of Python objects cannot be sent; convert to a numeric dtype or a list")
    if dt.itemsize == 0:
        raise TypeError(f"numpy arrays of zero-size dtype {dt} cannot be sent")
    if dt.fields is None and dt.subdtype is None:
        return dt.str
    return np.lib.format.dtype_to_descr(dt)  # structured dtype: list of field tuples


class _Encoder:
    """Per-thread encoder.  MessagePack's `default` hook cannot emit nested
    containers directly, so nested values (tuples, sets, structs) are packed by
    a second Packer; one Packer per nesting depth is kept and reused."""

    def __init__(self):
        self.buffers: list = []
        self._packers: list = []
        self._depth = 0

    def pack(self, obj) -> bytes:
        depth = self._depth
        if depth == len(self._packers):
            self._packers.append(msgpack.Packer(default=self._default, strict_types=True, use_bin_type=True))
        self._depth = depth + 1
        try:
            return self._packers[depth].pack(obj)
        finally:
            self._depth = depth

    def _default(self, o):
        if isinstance(o, tuple):
            return ExtType(_T_TUPLE, self.pack(list(o)))
        if isinstance(o, np.ndarray):
            return self._array(o)
        if isinstance(o, np.generic):
            if isinstance(o, np.complexfloating):
                return ExtType(_T_COMPLEX, _COMPLEX.pack(o.real, o.imag))
            return o.item()
        t = type(o)
        if t is _Oob:
            self.buffers.append(memoryview(o.data).cast("B"))
            return ExtType(_T_BLOB, _U32.pack(len(self.buffers) - 1))
        if t is complex:
            return ExtType(_T_COMPLEX, _COMPLEX.pack(o.real, o.imag))
        if t is set:
            return ExtType(_T_SET, self.pack(list(o)))
        if t is frozenset:
            return ExtType(_T_FROZENSET, self.pack(list(o)))
        entry = _by_type.get(t)
        if entry is not None:
            return ExtType(_T_STRUCT, self.pack([entry[0], entry[1](o)]))
        if t is Record:
            return ExtType(_T_STRUCT, self.pack([o.type_name, dict(o)]))
        if isinstance(o, enum.Enum):
            return o.value
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            register_type(t)
            entry = _by_type[t]
            return ExtType(_T_STRUCT, self.pack([entry[0], entry[1](o)]))
        # Subclasses of the basic types (strict_types sends them here): send as the base type.
        for base in (dict, list, str, int, float, bytes):
            if isinstance(o, base):
                return base(o)
        if isinstance(o, PurePath):
            return str(o)
        if isinstance(o, (set, frozenset)):
            return ExtType(_T_SET, self.pack(list(o)))
        raise TypeError(
            f"cannot send an object of type {t.__module__}.{t.__qualname__} over insthub "
            "(use plain data, numpy arrays, or a dataclass / insthub.register_type)")

    def _array(self, a: np.ndarray) -> ExtType:
        descr = _dtype_descr(a.dtype)
        shape = list(a.shape)
        if not a.flags.c_contiguous:
            a = np.ascontiguousarray(a)
        if a.nbytes <= INLINE_MAX:
            ref = a.tobytes()
        else:
            self.buffers.append(a.reshape(-1).view(np.uint8).data)
            ref = len(self.buffers) - 1
        return ExtType(_T_ARRAY, self.pack([descr, shape, ref]))


_tls = threading.local()


def _big(x) -> bool:
    t = type(x)
    return (t is bytes or t is bytearray) and len(x) > INLINE_MAX


def lift(obj):
    """Wrap large byte strings found at the top of `obj`, or one container level
    down, so that they travel out of band.  (MessagePack copies byte strings into
    the body; this keeps screenshots and waveform files zero-copy.)"""
    t = type(obj)
    if t is bytes or t is bytearray:
        return _Oob(obj) if len(obj) > INLINE_MAX else obj
    if (t is list or t is tuple) and len(obj) <= _LIFT_SCAN_MAX:
        if any(_big(x) for x in obj):
            return t(_Oob(x) if _big(x) else x for x in obj)
    elif t is dict and len(obj) <= _LIFT_SCAN_MAX:
        if any(_big(x) for x in obj.values()):
            return {k: (_Oob(x) if _big(x) else x) for k, x in obj.items()}
    return obj


def encode(obj) -> tuple[bytes, list]:
    """Serialize `obj`.  Returns (body, buffers); the buffers are views onto the
    caller's arrays, which must stay unmodified until they have been sent."""
    enc = getattr(_tls, "enc", None)
    if enc is None or enc._depth:  # first use on this thread, or a re-entrant call
        enc = _Encoder()
        if getattr(_tls, "enc", None) is None:
            _tls.enc = enc
    enc.buffers = buffers = []
    try:
        return enc.pack(obj), buffers
    finally:
        enc.buffers = []


# ---------------------------------------------------------------- decoding
_dtypes: dict[str, np.dtype] = {}


def _dtype(descr) -> np.dtype:
    if type(descr) is str:
        dt = _dtypes.get(descr)
        if dt is None:
            dt = _dtypes[descr] = np.dtype(descr)
        return dt
    return np.lib.format.descr_to_dtype(descr)


class _Decoder:
    __slots__ = ("buffers",)

    def __init__(self, buffers):
        self.buffers = buffers

    def unpack(self, data):
        return msgpack.unpackb(data, ext_hook=self._ext, strict_map_key=False)

    def _ext(self, code, data):
        if code == _T_TUPLE:
            return tuple(self.unpack(data))
        if code == _T_ARRAY:
            descr, shape, ref = self.unpack(data)
            dt = _dtype(descr)
            if type(ref) is int:  # out of band: the receive buffer becomes the array, no copy
                return self.buffers[ref].view(dt).reshape(shape)
            return np.frombuffer(ref, dtype=dt).reshape(shape).copy()
        if code == _T_BLOB:
            return self.buffers[_U32.unpack(data)[0]].tobytes()
        if code == _T_COMPLEX:
            return complex(*_COMPLEX.unpack(data))
        if code == _T_SET:
            return set(self.unpack(data))
        if code == _T_FROZENSET:
            return frozenset(self.unpack(data))
        if code == _T_STRUCT:
            name, fields = self.unpack(data)
            make = _by_name.get(name) or _find_loaded_dataclass(name)
            if make is not None:
                try:
                    return make(fields)
                except Exception:  # class changed shape between the two ends: degrade, don't fail
                    pass
            rec = Record(fields)
            rec.type_name = name
            return rec
        return ExtType(code, data)


def decode(body, buffers=()):
    """Inverse of `encode`.  `buffers` are uint8 numpy arrays (one per
    out-of-band buffer); arrays in the result are views onto them."""
    return _Decoder(buffers).unpack(body)

import dataclasses
import enum
from pathlib import Path

import numpy as np
import pytest

from insthub import Record, codec, register_type


def roundtrip(obj):
    body, buffers = codec.encode(obj)
    received = [np.frombuffer(bytes(b), np.uint8).copy() for b in buffers]  # as if read from a socket
    return codec.decode(body, received)


@pytest.mark.parametrize("value", [
    None, True, False, 0, -1, 2 ** 63 - 1, 1.5, float("inf"), "text", "µσ×", b"bytes", b"",
    [1, "a", None], (1, 2), (), [(1, 2), [3, (4,)]], {"a": 1, "b": [1, 2]}, {1: "int key", (1, 2): "tuple key"},
    {1, 2, 3}, frozenset({"a"}), 1 + 2j, {"nested": {"t": (1, {"x": (2.5, None)})}},
])
def test_plain_values_roundtrip(value):
    got = roundtrip(value)
    assert got == value and type(got) is type(value)


def test_nan():
    assert np.isnan(roundtrip(float("nan")))


def test_tuples_and_lists_stay_distinct():
    got = roundtrip({"t": (1, [2, (3,)]), "l": [1, (2,)]})
    assert type(got["t"]) is tuple and type(got["t"][1]) is list and type(got["t"][1][1]) is tuple
    assert type(got["l"]) is list and type(got["l"][1]) is tuple


@pytest.mark.parametrize("scalar, expected", [
    (np.float64(1.5), float), (np.float32(1.5), float), (np.int64(7), int), (np.uint8(7), int),
    (np.bool_(True), bool), (np.complex128(1 + 2j), complex), (np.str_("s"), str),
])
def test_numpy_scalars_become_python(scalar, expected):
    got = roundtrip([scalar])[0]
    assert type(got) is expected and got == scalar


def test_enum_path_and_subclasses():
    class Color(enum.IntEnum):
        RED = 3

    class Mode(enum.Enum):
        FAST = "fast"

    class MyDict(dict):
        pass

    assert roundtrip([Color.RED, Mode.FAST, Path("/tmp/x"), MyDict(a=1)]) == [3, "fast", "/tmp/x", {"a": 1}]


ARRAYS = [
    np.arange(10, dtype=np.float32),
    np.arange(100000, dtype=np.float64),
    np.zeros((0, 3), np.int16),
    np.array(3.5),
    np.arange(24, dtype=">i4").reshape(2, 3, 4),
    np.arange(20000, dtype=np.uint16).reshape(100, 200)[::2, ::3],          # not contiguous
    np.asfortranarray(np.arange(6000, dtype=np.int32).reshape(60, 100)),
    (np.arange(5000) + 1j * np.arange(5000)).astype(np.complex64),
    np.array([True, False] * 3000),
    np.array(["2026-01-01", "2026-11-24"], dtype="datetime64[D]"),
    np.zeros(1000, dtype=[("t", "<f8"), ("xy", "<i2", (2,)), ("ok", "?")]),
    np.array([b"ab", b"cdef"]),
    np.array(["x", "yz"]),
]


@pytest.mark.parametrize("arr", ARRAYS, ids=lambda a: f"{a.dtype}{list(a.shape)}")
def test_arrays_roundtrip(arr):
    got = roundtrip({"a": arr})["a"]
    assert got.dtype == arr.dtype and got.shape == arr.shape
    assert np.array_equal(got, arr)
    assert got.flags.writeable and got.flags.c_contiguous


def test_large_arrays_travel_out_of_band_without_copy():
    small, big = np.arange(10, dtype=np.float32), np.arange(100000, dtype=np.float32)
    body, buffers = codec.encode([small, big])
    assert len(buffers) == 1 and len(body) < 300
    assert buffers[0].nbytes == big.nbytes
    assert np.shares_memory(np.frombuffer(buffers[0], np.float32), big)   # a view, not a copy


def test_decoded_array_uses_the_receive_buffer():
    big = np.arange(100000, dtype=np.float32)
    body, buffers = codec.encode(big)
    received = np.frombuffer(bytes(buffers[0]), np.uint8).copy()
    got = codec.decode(body, [received])
    assert np.shares_memory(got, received)


def test_object_arrays_are_refused():
    with pytest.raises(TypeError, match="Python objects"):
        codec.encode(np.array([object()]))


def test_unsendable_objects_are_refused_with_a_clear_message():
    with pytest.raises(TypeError, match="cannot send an object of type"):
        codec.encode({"f": open})
    assert roundtrip([1, 2]) == [1, 2]          # the encoder is usable again afterwards


def test_big_bytes_go_out_of_band_and_come_back_as_bytes():
    blob = bytes(range(256)) * 1000
    for obj in (blob, [1, blob], {"png": blob}, (blob, bytearray(blob))):
        body, buffers = codec.encode(codec.lift(obj))
        assert buffers and len(body) < 200
        got = codec.decode(body, [np.frombuffer(bytes(b), np.uint8).copy() for b in buffers])
        flat = got if isinstance(got, bytes) else (list(got.values()) if isinstance(got, dict) else list(got))
        assert blob in (flat if isinstance(flat, list) else [flat])
    assert type(roundtrip(codec.lift(blob))) is bytes


@dataclasses.dataclass
class Trace:
    channel: int
    volts: np.ndarray
    meta: dict = dataclasses.field(default_factory=dict)


def test_dataclass_roundtrip():
    got = roundtrip(Trace(2, np.arange(5000, dtype=np.float32), {"dt": 1e-6}))
    assert type(got) is Trace and got.channel == 2 and got.meta == {"dt": 1e-6}
    assert np.array_equal(got.volts, np.arange(5000, dtype=np.float32))


def test_unknown_struct_arrives_as_record():
    body, buffers = codec.encode(Trace(1, np.zeros(3)))
    name = f"{Trace.__module__}.{Trace.__qualname__}"
    saved = codec._by_name.pop(name)
    body = body.replace(name.encode(), name.encode()[:-1] + b"X")       # a class this side has never seen
    try:
        got = codec.decode(body, [])
    finally:
        codec._by_name[name] = saved
    assert isinstance(got, Record) and got.channel == 1 and got["channel"] == 1
    assert got.type_name.endswith("TracX")
    again = roundtrip(got)                                              # and can be passed on
    assert isinstance(again, Record) and again.type_name == got.type_name


def test_register_custom_type():
    class Point:
        def __init__(self, x, y):
            self.x, self.y = x, y

    register_type(Point, name="test.Point", encode=lambda p: {"x": p.x, "y": p.y}, decode=lambda d: Point(**d))
    got = roundtrip([Point(1, 2.5)])[0]
    assert type(got) is Point and (got.x, got.y) == (1, 2.5)

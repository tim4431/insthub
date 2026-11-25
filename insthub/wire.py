"""Framing: how messages travel over a stream socket (TCP or Unix).

After connecting, each side sends the 8-byte `MAGIC`.  Then every message is

    u32 body_len | u16 nbuf | u8 type | u8 flags | u64 tag      (16 bytes, little-endian)
    u64 buffer_len  x nbuf
    body            MessagePack, see codec.py
    buffer bytes    x nbuf (raw array / blob memory)

`tag` is the request id for CALL / REPLY / ERROR (0 = no reply wanted) and the
subscription id for DATA.

    HELLO  server -> client   body: {"server", "pid", "devices", "protocol"}
    CALL   client -> server   body: [device, command, args, kwargs]
    REPLY  server -> client   body: the return value
    ERROR  server -> client   body: [class names, message, traceback]
    DATA   server -> client   body: [seq, time, data, meta]
"""
from __future__ import annotations

import socket
import struct

import numpy as np

MAGIC = b"INSTHUB1"
PROTOCOL = 1
HELLO, CALL, REPLY, ERROR, DATA = 1, 2, 3, 4, 5

_PREFIX = struct.Struct("<IHBBQ")
_PREFIX_SIZE = _PREFIX.size
_DONTWAIT = getattr(socket, "MSG_DONTWAIT", 0)
_HAS_SENDMSG = hasattr(socket.socket, "sendmsg")
#: A non-blocking send attempt is possible on this platform (see `try_send`).
CAN_TRY_SEND = bool(_DONTWAIT and _HAS_SENDMSG)
_IOV_MAX = 512
_SOCKBUF = 1 << 20


def frame(mtype: int, tag: int, body: bytes, buffers=()) -> list:
    """One message as a list of byte segments ready for a gather-write."""
    if not buffers:
        return [_PREFIX.pack(len(body), 0, mtype, 0, tag) + body]
    n = len(buffers)
    head = b"".join((_PREFIX.pack(len(body), n, mtype, 0, tag),
                     struct.pack(f"<{n}Q", *[b.nbytes for b in buffers]), body))
    return [head, *buffers]


def _advance(segs: list, n: int) -> list:
    """`segs` minus its first `n` bytes."""
    if n == 0:
        return segs
    i = 0
    for s in segs:
        size = len(s)
        if n < size:
            break
        n -= size
        i += 1
    rest = segs[i:]
    if n:
        rest[0] = memoryview(rest[0])[n:]
    return rest


def send_all(sock: socket.socket, segs: list) -> None:
    """Blocking write of every segment, without joining them."""
    if len(segs) == 1:
        sock.sendall(segs[0])
    elif not _HAS_SENDMSG:
        for s in segs:
            sock.sendall(s)
    else:
        while segs:
            segs = _advance(segs, sock.sendmsg(segs[:_IOV_MAX]))


def try_send(sock: socket.socket, segs: list) -> list:
    """Send as much as fits in the socket buffer right now, never blocking.
    Returns what is left: `[]` when done, `segs` itself when nothing went out."""
    try:
        if len(segs) == 1:
            n = sock.send(segs[0], _DONTWAIT)
        else:
            n = sock.sendmsg(segs[:_IOV_MAX], [], _DONTWAIT)
    except (BlockingIOError, InterruptedError):
        return segs
    return _advance(segs, n)


def tune(sock: socket.socket) -> None:
    """Low latency for small messages, large kernel buffers for bulk data."""
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, _SOCKBUF)
        except OSError:
            pass


class FrameReader:
    """Reads messages from a blocking socket.

    Small messages are parsed out of one receive buffer (usually one syscall per
    message, or one for a whole burst).  Large buffers are received directly
    into the uninitialized numpy array they will be decoded as."""

    def __init__(self, sock: socket.socket, bufsize: int = 1 << 16):
        self._sock = sock
        self._size = bufsize
        self._buf = bytearray(bufsize)
        self._mv = memoryview(self._buf)
        self._r = self._w = 0

    def _need(self, n: int) -> None:
        """Have `n` (<= bufsize) contiguous unread bytes at `_r`."""
        have = self._w - self._r
        if have >= n:
            return
        if have == 0:
            self._r = self._w = 0
        elif self._r + n > self._size:  # no room at the tail: move the unread part to the front
            self._buf[:have] = self._buf[self._r:self._w]
            self._r, self._w = 0, have
        recv_into, mv = self._sock.recv_into, self._mv
        while self._w - self._r < n:
            got = recv_into(mv[self._w:])
            if not got:
                raise EOFError("connection closed")
            self._w += got

    def _exact(self, n: int) -> np.ndarray:
        """`n` bytes as a new uint8 array, bypassing the receive buffer once it is drained."""
        out = np.empty(n, np.uint8)
        if n == 0:
            return out
        mv = memoryview(out)
        have = min(n, self._w - self._r)
        if have:
            mv[:have] = self._mv[self._r:self._r + have]
            self._r += have
        recv_into = self._sock.recv_into
        while have < n:
            got = recv_into(mv[have:])
            if not got:
                raise EOFError("connection closed")
            have += got
        return out

    def read_magic(self) -> None:
        self._need(len(MAGIC))
        got = bytes(self._mv[self._r:self._r + len(MAGIC)])
        self._r += len(MAGIC)
        if got != MAGIC:
            raise ConnectionError(f"peer does not speak insthub protocol {PROTOCOL} (got {got!r})")

    def read(self):
        """Next message as `(type, tag, body, buffers)`.  `body` may be a view
        into the receive buffer: decode it before reading again."""
        self._need(_PREFIX_SIZE)
        blen, nbuf, mtype, _flags, tag = _PREFIX.unpack_from(self._buf, self._r)
        self._r += _PREFIX_SIZE
        if nbuf:
            if 8 * nbuf <= self._size:
                self._need(8 * nbuf)
                sizes = struct.unpack_from(f"<{nbuf}Q", self._buf, self._r)
                self._r += 8 * nbuf
            else:
                sizes = struct.unpack(f"<{nbuf}Q", self._exact(8 * nbuf))
        if blen <= self._size:
            self._need(blen)
            body = self._mv[self._r:self._r + blen]
            self._r += blen
        else:
            body = self._exact(blen)
        buffers = [self._exact(n) for n in sizes] if nbuf else ()
        return mtype, tag, body, buffers


# ---------------------------------------------------------------- addresses
def parse_address(address: str) -> tuple[str, object]:
    """'tcp://host:port' -> ('tcp', (host, port));  'ipc:///path' -> ('ipc', path);
    'inproc://name' -> ('inproc', name)."""
    scheme, sep, rest = address.partition("://")
    if not sep:
        raise ValueError(f"bad address {address!r}: expected tcp://host:port, ipc:///path or inproc://name")
    if scheme == "tcp":
        host, _, port = rest.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError(f"bad address {address!r}: expected tcp://host:port")
        return "tcp", (host.strip("[]"), int(port))
    if scheme in ("ipc", "inproc"):
        if not rest:
            raise ValueError(f"bad address {address!r}")
        return scheme, rest
    raise ValueError(f"bad address {address!r}: unknown scheme {scheme!r}")


def open_socket(address: str, timeout: float | None) -> socket.socket:
    """Connect a client socket to a tcp:// or ipc:// address."""
    kind, target = parse_address(address)
    if kind == "tcp":
        sock = socket.create_connection(target, timeout=timeout)
    elif kind == "ipc":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(target)
        except BaseException:
            sock.close()
            raise
    else:
        raise ValueError(f"{address!r} is not a socket address")
    tune(sock)
    return sock

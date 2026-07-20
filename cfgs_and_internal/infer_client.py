"""
Thin client for the in-app TFLite GPU inference service (dev.pyla.app).

The Python bot runs as a detached process and cannot load a GPU delegate itself
(Android exposes the graphics stack only to app processes). So model inference is
hosted inside the app and reached over a loopback TCP socket. This client sends the
already-preprocessed NCHW float32 tensor and gets back the raw YOLO output, so all
letterbox/NMS/postprocess numpy in detect.py stays byte-identical.

Enabled only when the env var PYLA_INFER_ADDR ("host:port") is set (MainActivity sets
it at bot launch). If the service is down/slow, a circuit breaker trips and the caller
transparently falls back to the in-process ONNX Runtime session — the bot never stalls.

Wire format (little-endian, "PYLA" magic) — see project_pyla_gpu_inference_service memory.
"""

import socket
import struct
import time

import numpy as np

MAGIC = b"PYLA"
VERSION = 1
OP_INFER = 1
OP_HEALTH = 2
OP_INFO = 3
DTYPE_F32 = 0

STATUS_OK = 0
STATUS_ERR = 1
STATUS_WARMING = 2

_REQ_HEAD = struct.Struct("<4sBBBB")
_RESP_HEAD = struct.Struct("<4sBBBBB")
_BACKEND_NAMES = {0: "gpu", 1: "nnapi", 2: "xnnpack", 3: "cpu", 255: "n/a"}


class InferUnavailable(Exception):
    """Raised when the service is unreachable/slow; caller should use its fallback."""


class _Transport(Exception):
    """Internal: a protocol/framing problem, treated as a transport failure."""


class SocketInferBackend:
    def __init__(self, addr, model_key, connect_timeout=0.3, call_timeout=2.5, cooldown=5.0):
        host, _, port = addr.partition(":")
        self.host = host or "127.0.0.1"
        self.port = int(port)
        self.model_key = model_key
        self.key_bytes = model_key.encode("utf-8")
        self.connect_timeout = connect_timeout
        self.call_timeout = call_timeout
        self.base_cooldown = cooldown
        self._sock = None
        self._next_retry = 0.0
        self._cooldown = cooldown
        self.last_backend = None

    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(self.call_timeout)
        self._sock = s

    def _close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _trip(self):
        """Open the breaker with exponential backoff (capped)."""
        self._close()
        self._next_retry = time.monotonic() + self._cooldown
        self._cooldown = min(self._cooldown * 2, 30.0)

    def _recv_exact(self, n):
        buf = bytearray(n)
        view = memoryview(buf)
        got = 0
        while got < n:
            r = self._sock.recv_into(view[got:], n - got)
            if r == 0:
                raise InferUnavailable("connection closed mid-read")
            got += r
        return buf

    def infer(self, x):
        """x: NCHW float32 ndarray (the shared _padded_img_buffer). Returns [np.ndarray]
        (a 1-element list, matching ONNX Runtime's outputs list). Raises InferUnavailable."""
        now = time.monotonic()
        if now < self._next_retry:
            raise InferUnavailable("circuit open")

        x = np.ascontiguousarray(x, dtype=np.float32)
        payload = x.tobytes()
        dims = x.shape
        header = _REQ_HEAD.pack(MAGIC, VERSION, OP_INFER, DTYPE_F32, len(dims))
        header += b"".join(struct.pack("<i", d) for d in dims)
        header += struct.pack("<H", len(self.key_bytes)) + self.key_bytes
        header += struct.pack("<I", len(payload))

        try:
            if self._sock is None:
                self._connect()
            self._sock.sendall(header + payload)

            head = self._recv_exact(_RESP_HEAD.size)
            magic, ver, status, backend, dtype, ndim = _RESP_HEAD.unpack(head)
            if magic != MAGIC or ver != VERSION:
                raise _Transport("bad response magic/version")
            out_dims = struct.unpack("<%di" % ndim, self._recv_exact(4 * ndim)) if ndim else ()
            (plen,) = struct.unpack("<I", self._recv_exact(4))
            body = self._recv_exact(plen) if plen else b""
        except (OSError, socket.timeout, struct.error, ValueError, _Transport) as e:
            self._trip()
            raise InferUnavailable(str(e))

        if status == STATUS_OK:
            self.last_backend = _BACKEND_NAMES.get(backend, str(backend))
            self._cooldown = self.base_cooldown
            arr = np.frombuffer(bytes(body), dtype=np.float32).reshape(out_dims)
            return [arr]

        if status == STATUS_WARMING:
            raise InferUnavailable("warming")

        self._trip()
        raise InferUnavailable("service error status=%d: %s"
                               % (status, body.decode("utf-8", "replace")[:80]))


def query_backend(addr, model_key, timeout=1.0):
    """One-shot INFO request → the active backend name for a model, or None. For logging."""
    host, _, port = addr.partition(":")
    kb = model_key.encode("utf-8")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port)), timeout=timeout) as s:
            s.settimeout(timeout)
            req = _REQ_HEAD.pack(MAGIC, VERSION, OP_INFO, DTYPE_F32, 0)
            req += struct.pack("<H", len(kb)) + kb + struct.pack("<I", 0)
            s.sendall(req)
            head = s.recv(_RESP_HEAD.size)
            if len(head) < _RESP_HEAD.size or head[:4] != MAGIC:
                return None
            backend = head[3]
            return _BACKEND_NAMES.get(backend, str(backend))
    except (OSError, socket.timeout, ValueError):
        return None

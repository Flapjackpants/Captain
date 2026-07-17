"""Localhost JSON-RPC bridge between Resolve's script process and the UI.

Resolve Free (and Studio when launched from Workspace → Scripts) injects a
live `resolve` object into the script process. External `scriptapp()` calls
are Studio-only since Resolve 19.1, so Captain keeps all Resolve API traffic
inside the script process and exposes a tiny authenticated TCP JSON-RPC
server on 127.0.0.1 for the PySide6 UI.
"""

from __future__ import annotations

import json
import logging
import secrets
import socket
import socketserver
import threading
import traceback
from typing import Any, Callable

log = logging.getLogger("Captain.bridge")

PROTOCOL_VERSION = 1


class BridgeError(RuntimeError):
    pass


# ---- framing: one JSON object per newline ---------------------------------


def _encode(message: dict) -> bytes:
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def _read_message(sock: socket.socket, buf: bytearray) -> dict | None:
    """Read one newline-delimited JSON object. Returns None on clean EOF."""
    while True:
        newline = buf.find(b"\n")
        if newline >= 0:
            line = bytes(buf[:newline])
            del buf[: newline + 1]
            if not line.strip():
                continue
            return json.loads(line.decode("utf-8"))
        chunk = sock.recv(65536)
        if not chunk:
            return None
        buf.extend(chunk)


# ---- server -----------------------------------------------------------------


class _BridgeRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: BridgeServer = self.server  # type: ignore[assignment]
        buf = bytearray()
        authenticated = False
        try:
            while True:
                msg = _read_message(self.connection, buf)
                if msg is None:
                    return
                req_id = msg.get("id")
                method = msg.get("method")
                params = msg.get("params") or {}
                try:
                    if method == "auth":
                        if params.get("token") != server.token:
                            raise BridgeError("Invalid bridge token")
                        authenticated = True
                        result = {"ok": True, "protocol": PROTOCOL_VERSION}
                    elif not authenticated:
                        raise BridgeError("Not authenticated")
                    else:
                        result = server.dispatch(method, params)
                    self.wfile.write(_encode({"id": req_id, "result": result}))
                except Exception as e:
                    log.error("Bridge method %s failed: %s", method, traceback.format_exc())
                    self.wfile.write(
                        _encode({"id": req_id, "error": {"message": str(e)}})
                    )
        except (ConnectionResetError, BrokenPipeError, OSError):
            return


class BridgeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, dispatch: Callable[[str, dict], Any], token: str | None = None):
        self.dispatch = dispatch
        self.token = token or secrets.token_hex(16)
        super().__init__(("127.0.0.1", 0), _BridgeRequestHandler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def url(self) -> str:
        return f"127.0.0.1:{self.port}"

    def start(self) -> str:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        log.info("Bridge listening on %s", self.url)
        return self.url

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


# ---- client -----------------------------------------------------------------


class BridgeClient:
    def __init__(self, host: str, port: int, token: str, timeout: float = 120.0):
        self.host = host
        self.port = port
        self.token = token
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = bytearray()
        self._next_id = 1
        self._lock = threading.Lock()

    @classmethod
    def from_url(cls, url: str, token: str, timeout: float = 120.0) -> "BridgeClient":
        host, _, port_s = url.rpartition(":")
        if not host or not port_s:
            raise BridgeError(f"Invalid bridge URL: {url!r}")
        return cls(host, int(port_s), token, timeout=timeout)

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=10.0)
        sock.settimeout(self.timeout)
        self._sock = sock
        self._buf.clear()
        self.call("auth", {"token": self.token})

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def call(self, method: str, params: dict | None = None) -> Any:
        if self._sock is None:
            raise BridgeError("Bridge client is not connected")
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            self._sock.sendall(_encode({"id": req_id, "method": method, "params": params or {}}))
            while True:
                msg = _read_message(self._sock, self._buf)
                if msg is None:
                    raise BridgeError("Bridge connection closed by Resolve host")
                if msg.get("id") != req_id:
                    continue
                if "error" in msg:
                    raise BridgeError(msg["error"].get("message", "Unknown bridge error"))
                return msg.get("result")

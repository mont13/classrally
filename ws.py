"""Minimal WebSocket implementation using only Python stdlib.

Implements RFC 6455 for server-side WebSocket handling:
- Handshake (HTTP 101 Upgrade)
- Text frame encoding/decoding
- Ping/pong
- Close frames

No external dependencies — uses hashlib, base64, struct from stdlib.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import threading
import time
from typing import Any

# RFC 6455 magic GUID
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Opcodes
OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


def ws_accept_key(client_key: str) -> str:
    """Compute Sec-WebSocket-Accept value from client's Sec-WebSocket-Key."""
    combined = client_key.strip() + _WS_GUID
    sha1 = hashlib.sha1(combined.encode("ascii")).digest()
    return base64.b64encode(sha1).decode("ascii")


def ws_handshake_response(client_key: str) -> bytes:
    """Build the HTTP 101 Switching Protocols response."""
    accept = ws_accept_key(client_key)
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )
    return response.encode("ascii")


def ws_encode_text(payload: str) -> bytes:
    """Encode a text message as a WebSocket frame (server → client, unmasked)."""
    data = payload.encode("utf-8")
    frame = bytearray()
    frame.append(0x80 | OPCODE_TEXT)  # FIN + text opcode

    length = len(data)
    if length < 126:
        frame.append(length)
    elif length < 65536:
        frame.append(126)
        frame.extend(struct.pack("!H", length))
    else:
        frame.append(127)
        frame.extend(struct.pack("!Q", length))

    frame.extend(data)
    return bytes(frame)


def ws_encode_close(code: int = 1000, reason: str = "") -> bytes:
    """Encode a close frame."""
    payload = struct.pack("!H", code) + reason.encode("utf-8")[:123]
    frame = bytearray()
    frame.append(0x80 | OPCODE_CLOSE)
    frame.append(len(payload))
    frame.extend(payload)
    return bytes(frame)


def ws_encode_ping(data: bytes = b"") -> bytes:
    """Encode a ping frame."""
    frame = bytearray()
    frame.append(0x80 | OPCODE_PING)
    frame.append(len(data))
    frame.extend(data)
    return bytes(frame)


def ws_encode_pong(data: bytes = b"") -> bytes:
    """Encode a pong frame."""
    frame = bytearray()
    frame.append(0x80 | OPCODE_PONG)
    frame.append(len(data))
    frame.extend(data)
    return bytes(frame)


def ws_decode_frame(data: bytes) -> tuple[int, bytes, int] | None:
    """Decode a WebSocket frame from raw bytes.

    Returns (opcode, payload, bytes_consumed) or None if not enough data.
    Client frames are always masked (RFC 6455).
    """
    if len(data) < 2:
        return None

    byte0 = data[0]
    byte1 = data[1]

    opcode = byte0 & 0x0F
    masked = (byte1 & 0x80) != 0
    length = byte1 & 0x7F

    offset = 2
    if length == 126:
        if len(data) < 4:
            return None
        length = struct.unpack("!H", data[2:4])[0]
        offset = 4
    elif length == 127:
        if len(data) < 10:
            return None
        length = struct.unpack("!Q", data[2:10])[0]
        offset = 10

    if masked:
        if len(data) < offset + 4:
            return None
        mask_key = data[offset:offset + 4]
        offset += 4

    if len(data) < offset + length:
        return None

    payload = bytearray(data[offset:offset + length])
    if masked:
        for i in range(length):
            payload[i] ^= mask_key[i % 4]

    return opcode, bytes(payload), offset + length


class WSConnection:
    """Represents a single WebSocket connection."""

    def __init__(self, sock: Any, conn_id: str,
                 player_id: str | None = None, is_host: bool = False):
        self.sock = sock
        self.conn_id = conn_id
        self.player_id = player_id
        self.is_host = is_host
        self.alive = True
        self.last_pong = time.time()
        self._send_lock = threading.Lock()

    def send_text(self, text: str) -> bool:
        """Send a text frame. Returns False if connection is dead."""
        if not self.alive:
            return False
        try:
            with self._send_lock:
                self.sock.sendall(ws_encode_text(text))
            return True
        except (OSError, BrokenPipeError):
            self.alive = False
            return False

    def send_json(self, data: Any) -> bool:
        """Send JSON data as text frame."""
        return self.send_text(json.dumps(data, ensure_ascii=False))

    def send_ping(self) -> bool:
        """Send a ping frame."""
        if not self.alive:
            return False
        try:
            with self._send_lock:
                self.sock.sendall(ws_encode_ping(b"ping"))
            return True
        except (OSError, BrokenPipeError):
            self.alive = False
            return False

    def send_close(self, code: int = 1000) -> None:
        """Send a close frame and mark connection dead."""
        try:
            with self._send_lock:
                self.sock.sendall(ws_encode_close(code))
        except (OSError, BrokenPipeError):
            pass
        self.alive = False

    def close(self) -> None:
        """Close the underlying socket."""
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass


class WSConnectionManager:
    """Manages all active WebSocket connections."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connections: dict[str, WSConnection] = {}
        self._state_getter: Any = None  # Callable to get game state

    def set_state_getter(self, getter: Any) -> None:
        """Set function to get current game state: (player_id, host_view) -> dict."""
        self._state_getter = getter

    def register(self, conn: WSConnection) -> None:
        """Register a new WebSocket connection."""
        with self._lock:
            self._connections[conn.conn_id] = conn

    def unregister(self, conn_id: str) -> None:
        """Remove a connection."""
        with self._lock:
            conn = self._connections.pop(conn_id, None)
            if conn:
                conn.close()

    def broadcast_state(self) -> None:
        """Push current state to all connected clients."""
        if not self._state_getter:
            return

        with self._lock:
            conns = list(self._connections.values())

        dead = []
        for conn in conns:
            if not conn.alive:
                dead.append(conn.conn_id)
                continue
            try:
                state = self._state_getter(
                    player_id=conn.player_id,
                    host_view=conn.is_host,
                )
                if not conn.send_json(state):
                    dead.append(conn.conn_id)
            except Exception:
                dead.append(conn.conn_id)

        # Clean up dead connections
        if dead:
            with self._lock:
                for cid in dead:
                    c = self._connections.pop(cid, None)
                    if c:
                        c.close()

    def ping_all(self) -> None:
        """Send ping to all connections, remove unresponsive ones."""
        with self._lock:
            conns = list(self._connections.values())

        dead = []
        now = time.time()
        for conn in conns:
            if not conn.alive:
                dead.append(conn.conn_id)
                continue
            # If no pong in 60s, consider dead
            if now - conn.last_pong > 60:
                dead.append(conn.conn_id)
                continue
            conn.send_ping()

        if dead:
            with self._lock:
                for cid in dead:
                    c = self._connections.pop(cid, None)
                    if c:
                        c.close()

    @property
    def connection_count(self) -> int:
        with self._lock:
            return len(self._connections)

    def read_loop(self, conn: WSConnection) -> None:
        """Read loop for a single WebSocket connection. Runs in its own thread."""
        buf = bytearray()
        try:
            while conn.alive:
                try:
                    chunk = conn.sock.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf.extend(chunk)

                while True:
                    result = ws_decode_frame(bytes(buf))
                    if result is None:
                        break
                    opcode, payload, consumed = result
                    buf = buf[consumed:]

                    if opcode == OPCODE_CLOSE:
                        conn.send_close()
                        conn.alive = False
                        break
                    elif opcode == OPCODE_PING:
                        try:
                            with conn._send_lock:
                                conn.sock.sendall(ws_encode_pong(payload))
                        except (OSError, BrokenPipeError):
                            conn.alive = False
                            break
                    elif opcode == OPCODE_PONG:
                        conn.last_pong = time.time()
                    elif opcode == OPCODE_TEXT:
                        # Client sent a text message — could be used for future features
                        pass

        finally:
            self.unregister(conn.conn_id)

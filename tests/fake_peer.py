"""A fake bot for loopback tests: webhook receiver plus dial-back peer.

It is deliberately small and stdlib-only. On every POST it records the
payload and dials reply.host:reply.port, sends ack/status and an echo answer,
then reads the CLI's frames until the socket closes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

ALG = "hmac-sha256-v1"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def frame_mac(key: bytes, from_name: Optional[str], body: bytes,
              queued_at: Optional[str] = None) -> bytes:
    if queued_at is None:
        if from_name is None:
            data = body
        else:
            data = from_name.encode("utf-8") + b"\n" + body
    else:
        if from_name is None:
            raise ValueError("queued_at requires from")
        data = (
            from_name.encode("utf-8")
            + b"\x00"
            + queued_at.encode("utf-8")
            + b"\n"
            + body
        )
    return hmac.new(key, data, hashlib.sha256).digest()


def make_frame(kind: str, body: str, key: bytes, session_id: str,
               from_name: str = "Soul") -> bytes:
    raw = body.encode("utf-8")
    obj = {
        "v": 1,
        "alg": ALG,
        "kind": kind,
        "session_id": session_id,
        "from": from_name,
        "mac_b64": _b64(frame_mac(key, from_name, raw)),
        "body_b64": _b64(raw),
    }
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


class _Handler(BaseHTTPRequestHandler):
    peer: "FakeBot"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            self.peer._on_webhook(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")
        except Exception as exc:  # keep the test failure visible
            self.peer.errors.append(f"webhook handler: {exc!r}")
            try:
                self.send_response(500)
                self.end_headers()
            except OSError:
                pass

    def log_message(self, fmt: str, *args: Any) -> None:
        return


class FakeBot:
    """A fake main bot. Start it, point a webhook at ``url``, call stop()."""

    def __init__(
        self,
        *,
        bot_name: str = "Soul",
        first_delay: float = 0.0,
        relay_to: Optional[str] = None,
        close_on: Optional[str] = None,
        idle_ping: Optional[str] = None,
    ) -> None:
        self.bot_name = bot_name
        self.first_delay = first_delay
        self.relay_to = relay_to
        self.close_on = close_on
        self.idle_ping = idle_ping
        self.payloads: list[dict[str, Any]] = []
        self.tcp_messages: list[str] = []
        self.errors: list[str] = []
        self._lock = threading.Lock()
        self._socks: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        _Handler.peer = self
        self._server_thread = threading.Thread(
            target=self._server.serve_forever, name="fake-webhook", daemon=True
        )

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook"

    def start(self) -> None:
        self._server_thread.start()

    def stop(self) -> None:
        try:
            self._server.shutdown()
        except Exception:
            pass
        try:
            self._server.server_close()
        except Exception:
            pass
        with self._lock:
            socks = list(self._socks)
            self._socks.clear()
        for sock in socks:
            try:
                sock.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=2)
        self._server_thread.join(timeout=2)

    # -- waits ----------------------------------------------------------

    def wait_for_webhook(self, needle: str, timeout: float = 10.0) -> bool:
        return self._wait(
            lambda: [
                p.get("message", "")
                for p in self.payloads
                if needle in str(p.get("message", ""))
            ],
            timeout,
        )

    def wait_for_tcp(self, needle: str, timeout: float = 10.0) -> bool:
        return self._wait(
            lambda: [m for m in self.tcp_messages if needle in m], timeout
        )

    def _wait(self, probe, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if probe():
                    return True
            time.sleep(0.02)
        with self._lock:
            return bool(probe())

    # -- internals ------------------------------------------------------

    def _on_webhook(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.payloads.append(payload)
        thread = threading.Thread(
            target=self._dial_and_serve,
            args=(payload,),
            name=f"fake-dial-{len(self.payloads)}",
            daemon=True,
        )
        self._threads.append(thread)
        thread.start()

    def _dial_and_serve(self, payload: dict[str, Any]) -> None:
        reply = payload.get("reply") or {}
        try:
            key = base64.b64decode(reply["crypto"]["key_b64"])
            session_id = str(payload["session_id"])
            message = str(payload.get("message", ""))
            sock = socket.create_connection(
                (reply["host"], int(reply["port"])), timeout=5
            )
        except Exception as exc:
            self.errors.append(f"dial: {exc!r}")
            return
        with self._lock:
            self._socks.append(sock)
        try:
            parts = [make_frame("ack", "", key, session_id, self.bot_name)]
            parts.append(
                make_frame("status", "accepted", key, session_id, self.bot_name)
            )
            sock.sendall(b"".join(parts))

            if self.first_delay:
                time.sleep(self.first_delay)
            if self.relay_to:
                sock.sendall(
                    make_frame(
                        "relayed", self.relay_to, key, session_id, self.bot_name
                    )
                )
            if self.close_on is not None and message == self.close_on:
                sock.sendall(
                    make_frame("close", "test done", key, session_id, self.bot_name)
                )
                return
            sock.sendall(
                make_frame(
                    "msg", f"echo: {message}\n", key, session_id, self.bot_name
                )
            )
            self._serve_frames(sock, key, session_id)
        except OSError as exc:
            self.errors.append(f"serve: {exc!r}")
        finally:
            with self._lock:
                if sock in self._socks:
                    self._socks.remove(sock)
            try:
                sock.close()
            except OSError:
                pass

    def _serve_frames(
        self, sock: socket.socket, key: bytes, session_id: str
    ) -> None:
        sock.setblocking(False)
        buf = bytearray()
        ping_at = time.monotonic() + 3.0 if self.idle_ping else None
        while True:
            now = time.monotonic()
            timeout = 0.2
            if ping_at is not None:
                timeout = max(0.0, min(timeout, ping_at - now))
            try:
                ready, _, _ = select.select([sock], [], [], timeout)
            except (OSError, ValueError):
                return
            if self.idle_ping and ping_at is not None and time.monotonic() >= ping_at:
                ping_at = None
                try:
                    sock.setblocking(True)
                    sock.sendall(
                        make_frame(
                            "msg",
                            self.idle_ping + "\n",
                            key,
                            session_id,
                            self.bot_name,
                        )
                    )
                    sock.setblocking(False)
                except OSError:
                    return
            if not ready:
                continue
            try:
                chunk = sock.recv(65536)
            except BlockingIOError:
                continue
            except OSError:
                return
            if not chunk:
                return
            buf.extend(chunk)
            while b"\n" in buf:
                raw, _, rest = bytes(buf).partition(b"\n")
                buf = bytearray(rest)
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.errors.append("bad json from CLI")
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("alg") != ALG:
                    self.errors.append("bad alg from CLI")
                    continue
                try:
                    body = base64.b64decode(obj["body_b64"])
                    mac = base64.b64decode(obj["mac_b64"])
                except Exception:
                    self.errors.append("bad base64 from CLI")
                    continue
                from_name = obj.get("from")
                if from_name is not None and not isinstance(from_name, str):
                    self.errors.append("bad from from CLI")
                    continue
                expected = frame_mac(key, from_name, body)
                if not hmac.compare_digest(mac, expected):
                    self.errors.append("bad mac from CLI")
                    continue
                kind = obj.get("kind")
                text = body.decode("utf-8", "replace")
                if kind == "msg":
                    value = text[:-1] if text.endswith("\n") else text
                    with self._lock:
                        self.tcp_messages.append(value)
                    if self.close_on is not None and value == self.close_on:
                        sock.setblocking(True)
                        sock.sendall(
                            make_frame(
                                "close",
                                "test done",
                                key,
                                session_id,
                                self.bot_name,
                            )
                        )
                        return
                    sock.setblocking(True)
                    sock.sendall(
                        make_frame(
                            "msg",
                            f"echo: {value}\n",
                            key,
                            session_id,
                            self.bot_name,
                        )
                    )
                    sock.setblocking(False)
                elif kind == "close":
                    return

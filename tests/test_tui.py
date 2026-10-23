#!/usr/bin/env python3
"""TUI and CLI regression tests (stdlib only, loopback).

Each test runs the real bin/asksoul under a pty against fake_peer.FakeBot
with a throwaway HOME. bin/asksoul is also loaded with SourceFileLoader so
the tests can use its VERSION and configuration helpers directly.
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import shutil
import struct
import subprocess
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path

import fake_peer
from support import ASKSOUL, kill_session_daemons, load_asksoul, make_env

ASKSOUL_MOD = load_asksoul()
VERSION = ASKSOUL_MOD.VERSION

ANSI_RE = re.compile(
    rb"\x1b(?:\[[0-9;?]*[ -/]*[@-~]"
    rb"|\][^\x07]*(?:\x07|\x1b\\)"
    rb"|[()][A-Za-z0-9]"
    rb"|[=>78])"
)


def strip_ansi(data: bytes) -> bytes:
    return ANSI_RE.sub(b"", data)


class PtyRun:
    """asksoul under a pty; all three std fds share the slave side."""

    def __init__(
        self, args: list[str], env: dict[str, str], rows: int = 30, cols: int = 100
    ) -> None:
        self.master, slave = pty.openpty()
        fcntl.ioctl(
            slave,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, cols, 0, 0),
        )
        self.proc = subprocess.Popen(
            [str(ASKSOUL), *args],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            close_fds=True,
        )
        os.close(slave)
        self._chunks: list[bytes] = []
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        while True:
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return
            if not chunk:
                return
            with self._lock:
                self._chunks.append(chunk)

    def output(self) -> bytes:
        with self._lock:
            return b"".join(self._chunks)

    def text(self) -> str:
        return strip_ansi(self.output()).decode("utf-8", "replace")

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def wait_for(self, needle: str, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.text():
                return True
            time.sleep(0.05)
        return False

    def finish(self, timeout: float = 15.0) -> int | None:
        rc: int | None
        try:
            rc = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
            rc = None
        self._reader.join(timeout=2)
        try:
            os.close(self.master)
        except OSError:
            pass
        return rc


class TuiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="asksoul-tui-test-"))
        self.fakes: list[fake_peer.FakeBot] = []
        self.runs: list[PtyRun] = []

    def tearDown(self) -> None:
        for run in self.runs:
            run.finish(5)
        for fake in self.fakes:
            fake.stop()
        kill_session_daemons(self.home / "state")
        shutil.rmtree(self.home, ignore_errors=True)

    def fake(self, **kwargs) -> fake_peer.FakeBot:
        bot = fake_peer.FakeBot(**kwargs)
        bot.start()
        self.fakes.append(bot)
        return bot

    def pty(self, args: list[str], env: dict[str, str]) -> PtyRun:
        run = PtyRun(args, env)
        self.runs.append(run)
        return run

    # -- basic ----------------------------------------------------------

    def test_help_and_version(self) -> None:
        main_help = subprocess.run(
            [str(ASKSOUL), "-h"], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(main_help.returncode, 0, main_help.stderr)
        self.assertIn("tui [-s ID]", main_help.stdout)
        self.assertIn("\nTUI\n---\n", main_help.stdout)
        tui_help = subprocess.run(
            [str(ASKSOUL), "tui", "-h"], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(tui_help.returncode, 0)
        self.assertIn("tui [-s ID]", tui_help.stdout)
        version = subprocess.run(
            [str(ASKSOUL), "--version"], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(version.stdout.strip(), f"asksoul {VERSION}")

    def test_non_tty(self) -> None:
        proc = subprocess.run(
            [str(ASKSOUL), "tui"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            env=make_env(self.home, "http://127.0.0.1:1/hook"),
        )
        self.assertEqual(proc.returncode, 2, proc.returncode)
        self.assertIn("asksoul tui needs a TTY", proc.stderr)
        self.assertEqual(proc.stdout, "")

    # -- TUI flows ------------------------------------------------------

    def test_roundtrip(self) -> None:
        fake = self.fake(first_delay=1.5, relay_to="Designer")
        env = make_env(self.home, fake.url)
        run = self.pty(["tui", "-w", "tester", "--wait", "30"], env)

        self.assertTrue(run.wait_for("asksoul tui", 10), run.text()[-3000:])
        id_hits = re.findall(
            r"asksoul: session [0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}",
            run.text(),
        )
        self.assertEqual(len(id_hits), 1, "session id printed more than once")
        self.assertTrue(run.wait_for("who: tester", 5), run.text()[-3000:])
        self.assertEqual(fake.payloads, [], "no session may open before Enter")

        run.send(b"junk")
        time.sleep(0.2)
        run.send(b"\x1b")
        time.sleep(0.2)
        run.send(b"hello\n")
        self.assertTrue(fake.wait_for_webhook("hello", 15), fake.errors)
        self.assertEqual([p.get("message") for p in fake.payloads], ["hello"])
        self.assertEqual(fake.payloads[0].get("sender_type"), "user-tui")
        self.assertTrue(run.wait_for("waiting…", 3), run.text()[-3000:])
        self.assertTrue(run.wait_for("# relayed: Designer (by Soul)", 15),
                        run.text()[-3000:])
        self.assertTrue(run.wait_for("echo: hello", 15), run.text()[-3000:])

        run.send(b"again\n")
        self.assertTrue(fake.wait_for_tcp("again", 15), fake.errors)
        self.assertTrue(run.wait_for("echo: again", 15), run.text()[-3000:])

        run.send(b"/bogus\n")
        self.assertTrue(run.wait_for("unknown command", 5), run.text()[-3000:])

        run.send(b"\x1b[5~")
        run.send(b"\x1b[6~")
        fcntl.ioctl(
            run.master, termios.TIOCSWINSZ, struct.pack("HHHH", 4, 20, 0, 0)
        )
        time.sleep(0.2)
        self.assertIsNone(run.proc.poll(), "tiny resize must not exit")
        fcntl.ioctl(
            run.master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0)
        )
        run.send(b"x" * 250 + "你好世界".encode("utf-8") * 10)
        time.sleep(0.4)
        self.assertIsNone(run.proc.poll(), "long input must not exit")
        run.send(b"\x1b")
        time.sleep(0.2)
        self.assertIsNone(run.proc.poll(), "scroll/resize/input must not exit")

        run.send(b"/wait\n")
        self.assertTrue(
            run.wait_for("waiting for the next inbound message", 5),
            run.text()[-3000:],
        )
        time.sleep(0.3)
        run.send(b"\x03")
        self.assertTrue(run.wait_for("# cancelled", 5), run.text()[-3000:])
        self.assertIsNone(run.proc.poll(), "Ctrl-C during a wait must not exit")

        run.send(b"/help\n")
        self.assertTrue(run.wait_for("tui commands:", 5), run.text()[-3000:])
        run.send(b"/clear\n")
        time.sleep(0.3)
        run.send(b"/quit\n")
        rc = run.finish(15)
        self.assertEqual(rc, 0, run.text()[-3000:])

        self.assertEqual([p.get("message") for p in fake.payloads], ["hello"])
        self.assertEqual(fake.tcp_messages, ["again"], fake.tcp_messages)
        self.assertEqual(fake.errors, [])
        out = run.text()
        self.assertIn("[you] hello", out)
        self.assertIn("[you] again", out)
        self.assertIn("# ack (by Soul)", out)
        self.assertIn("# status: accepted", out)

    def test_cli_regression(self) -> None:
        fake = self.fake()
        env = make_env(self.home, fake.url)
        proc = subprocess.run(
            [
                str(ASKSOUL),
                "-s",
                "cli-regress",
                "-w",
                "tester",
                "-t",
                "30",
                "--wait",
                "30",
                "hello-cli",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            proc.returncode, 0, (proc.returncode, proc.stdout, proc.stderr)
        )
        self.assertIn("[Soul] echo: hello-cli", proc.stdout)
        self.assertIn("DELIVERY ack", proc.stdout)
        self.assertNotRegex(
            proc.stderr,
            r"asksoul: session [0-9a-f-]{36}",
            "-s must not print a generated id",
        )

        bad = self.pty(["tui", "-s", "cli-regress", "-w", "x"], env)
        bad_rc = bad.finish(10)
        self.assertEqual(bad_rc, 1, bad.text()[-2000:])
        self.assertIn("only apply when a session opens", bad.text())

        closed = subprocess.run(
            [str(ASKSOUL), "close", "cli-regress"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertTrue(fake.wait_for_webhook("hello-cli", 5))

    def test_attach_live(self) -> None:
        fake = self.fake(idle_ping="idle ping")
        env = make_env(self.home, fake.url)
        proc = subprocess.run(
            [
                str(ASKSOUL),
                "-s",
                "live-tui",
                "-t",
                "30",
                "--wait",
                "30",
                "start",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("[Soul] echo: start", proc.stdout)

        run = self.pty(["tui", "-s", "live-tui", "--wait", "30"], env)
        self.assertTrue(
            run.wait_for("attached to session live-tui", 10),
            run.text()[-3000:],
        )
        run.send(b"later\n")
        self.assertTrue(fake.wait_for_tcp("later", 15), fake.errors)
        self.assertTrue(run.wait_for("echo: later", 15), run.text()[-3000:])
        self.assertTrue(run.wait_for("idle ping", 10), run.text()[-3000:])
        run.send(b"/quit\n")
        rc = run.finish(15)
        self.assertEqual(rc, 0, run.text()[-3000:])
        self.assertEqual([p.get("message") for p in fake.payloads], ["start"])
        self.assertEqual(fake.tcp_messages, ["later"], fake.tcp_messages)

    def test_remote_close_and_reopen(self) -> None:
        fake = self.fake(close_on="bye")
        env = make_env(self.home, fake.url)
        run = self.pty(["tui", "--wait", "30"], env)
        self.assertTrue(run.wait_for("asksoul tui", 10), run.text()[-3000:])
        run.send(b"hello\n")
        self.assertTrue(run.wait_for("echo: hello", 15), run.text()[-3000:])
        run.send(b"bye\n")
        self.assertTrue(
            run.wait_for("# session closed (test done)", 10), run.text()[-3000:]
        )
        time.sleep(0.3)
        self.assertIsNone(run.proc.poll(), "a remote close must not kill the TUI")

        run.send(b"reopen\n")
        self.assertTrue(fake.wait_for_webhook("reopen", 15), fake.errors)
        self.assertTrue(run.wait_for("echo: reopen", 15), run.text()[-3000:])
        run.send(b"/quit\n")
        rc = run.finish(15)
        self.assertEqual(rc, 0, run.text()[-3000:])
        self.assertEqual(
            [p.get("message") for p in fake.payloads], ["hello", "reopen"]
        )
        self.assertEqual(fake.tcp_messages, ["bye"], fake.tcp_messages)
        self.assertEqual(fake.errors, [])

    def test_wait_timeout(self) -> None:
        fake = self.fake()
        env = make_env(self.home, fake.url)
        run = self.pty(["tui", "--wait", "1"], env)
        self.assertTrue(run.wait_for("asksoul tui", 10), run.text()[-3000:])
        run.send(b"hi\n")
        self.assertTrue(run.wait_for("echo: hi", 15), run.text()[-3000:])
        run.send(b"/wait\n")
        self.assertTrue(
            run.wait_for("nothing yet (still listening)", 6), run.text()[-3000:]
        )
        run.send(b"/close\n")
        rc = run.finish(15)
        self.assertEqual(rc, 0, run.text()[-3000:])

    def test_idle_ctrl_c(self) -> None:
        fake = self.fake()
        env = make_env(self.home, fake.url)
        run = self.pty(["tui", "--wait", "30"], env)
        self.assertTrue(run.wait_for("asksoul tui", 10), run.text()[-3000:])
        run.send(b"hello\n")
        self.assertTrue(run.wait_for("echo: hello", 15), run.text()[-3000:])
        run.send(b"\x03")
        rc = run.finish(15)
        self.assertEqual(rc, 0, run.text()[-3000:])
        sessions = self.home / "state" / "asksoul" / "sessions"
        deadline = time.monotonic() + 5
        leftover = list(sessions.iterdir()) if sessions.is_dir() else []
        while leftover and time.monotonic() < deadline:
            time.sleep(0.05)
            leftover = list(sessions.iterdir()) if sessions.is_dir() else []
        self.assertEqual(leftover, [], f"session dir still present: {leftover}")

    def test_bracketed_paste(self) -> None:
        fake = self.fake()
        env = make_env(self.home, fake.url)
        run = self.pty(["tui", "-w", "tester", "--wait", "30"], env)
        self.assertTrue(run.wait_for("asksoul tui", 10), run.text()[-3000:])
        run.send(b"\x1b[200~line1\nline2\x1b[201~\n")
        self.assertTrue(
            fake.wait_for_webhook("line1\nline2", 15),
            (fake.payloads, fake.errors, run.text()[-3000:]),
        )
        self.assertEqual(
            [p.get("message") for p in fake.payloads], ["line1\nline2"]
        )
        self.assertTrue(run.wait_for("echo: line1", 15), run.text()[-3000:])
        run.send(b"solo\n")
        self.assertTrue(fake.wait_for_tcp("solo", 15), fake.errors)
        self.assertTrue(run.wait_for("echo: solo", 15), run.text()[-3000:])
        run.send(b"/quit\n")
        rc = run.finish(15)
        self.assertEqual(rc, 0, run.text()[-3000:])
        self.assertEqual(fake.tcp_messages, ["solo"], fake.tcp_messages)
        self.assertEqual(fake.errors, [])


if __name__ == "__main__":
    unittest.main()

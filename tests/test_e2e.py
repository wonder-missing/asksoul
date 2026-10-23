#!/usr/bin/env python3
"""Loopback end-to-end tests: the real CLI and the real asksoul-talk.

The CLI runs with ASKSOUL_NO_WEBHOOK=1, binds 127.0.0.1 on an ephemeral port
and prints the dial-back triple on stderr. The bot helper then dials in with
real signed frames, exactly as the routine prompt says.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from support import ASKSOUL, TALK, clean_env, kill_session_daemons, make_env


class CliProc:
    """Run the CLI and capture stderr incrementally (for the no-webhook line)."""

    def __init__(self, args: list[str], env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            [str(ASKSOUL), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self.lines: list[str] = []
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            with self._lock:
                self.lines.append(line)

    def stderr_text(self) -> str:
        with self._lock:
            return "".join(self.lines)

    def wait_info(self, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                lines = list(self.lines)
            for line in lines:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("no_webhook"):
                    return obj
            if self.proc.poll() is not None:
                break
            time.sleep(0.02)
        raise AssertionError(
            f"CLI did not print the dial-back info; stderr={self.stderr_text()!r}"
        )

    def finish(self, timeout: float = 30.0) -> tuple[int, str, str]:
        rc = self.proc.wait(timeout=timeout)
        assert self.proc.stdout is not None
        out = self.proc.stdout.read()
        self._reader.join(timeout=2)
        err = self.stderr_text()
        self._close_pipes()
        return rc, out, err

    def _close_pipes(self) -> None:
        for pipe in (self.proc.stdout, self.proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self._reader.join(timeout=2)
        self._close_pipes()


class E2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="asksoul-e2e-")
        self.home = Path(self.tmp.name)
        self.env = make_env(
            self.home,
            "http://127.0.0.1:1/unused",
            extra={"ASKSOUL_NO_WEBHOOK": "1"},
        )
        self.talk_env = clean_env()
        self.talk_env.update(
            {
                "HOME": str(self.home),
                "XDG_STATE_HOME": str(self.home / "state"),
                "ASKSOUL_BOT_NAME": "Soul",
                "ASKSOUL_DRAIN_SEC": "0.5",
                "ASKSOUL_TCP_TIMEOUT": "5",
            }
        )
        self.procs: list[CliProc] = []

    def tearDown(self) -> None:
        for proc in self.procs:
            proc.kill()
        kill_session_daemons(self.home / "state")
        self.tmp.cleanup()

    def start_cli(self, *args: str) -> CliProc:
        proc = CliProc(list(args), self.env)
        self.procs.append(proc)
        return proc

    def endpoint(self, info: dict) -> list[str]:
        return [
            "--host",
            str(info["host"]),
            "--port",
            str(info["port"]),
            "--key-b64",
            str(info["key_b64"]),
            "--session",
            str(info["session_id"]),
            "--from",
            "Soul",
        ]

    def talk(self, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(TALK), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self.talk_env,
        )

    def close_session(self, session_id: str) -> None:
        closed = subprocess.run(
            [str(ASKSOUL), "close", session_id],
            capture_output=True,
            text=True,
            timeout=30,
            env=self.env,
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)

    # -- tests ----------------------------------------------------------

    def test_send_and_delivery(self) -> None:
        cli = self.start_cli(
            "-s", "e2e-send", "--wait", "20", "hello from the caller"
        )
        info = cli.wait_info()
        self.assertEqual(info["host"], "127.0.0.1")
        self.assertEqual(info["session_id"], "e2e-send")
        self.assertEqual(len(info["key_b64"]), 44)  # 32 bytes, base64

        talk = self.talk(
            "send",
            *self.endpoint(info),
            "--body",
            "the answer",
        )
        self.assertEqual(talk.returncode, 0, talk.stderr)
        self.assertIn("DELIVERY ack", talk.stdout)

        rc, out, err = cli.finish()
        self.assertEqual(rc, 0, (rc, out, err))
        self.assertIn("[Soul] the answer", out)
        self.assertIn("# ack (by Soul)", out)
        self.assertIn("DELIVERY ack", out)
        self.close_session("e2e-send")

    def test_ask_multi_turn(self) -> None:
        sid = "e2e-ask"
        cli1 = self.start_cli("-s", sid, "--wait", "20", "please start")
        info = cli1.wait_info()

        talk1 = subprocess.Popen(
            [
                str(TALK),
                "ask",
                *self.endpoint(info),
                "--wait",
                "20",
                "--body",
                "what is your name?",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.talk_env,
        )
        rc1, out1, err1 = cli1.finish()
        self.assertEqual(rc1, 0, (rc1, out1, err1))
        self.assertIn("[Soul] what is your name?", out1)

        # The caller's next line goes over the held TCP connection.
        cli2 = self.start_cli("-s", sid, "--wait", "20", "Alice")
        t1out, t1err = talk1.communicate(timeout=20)
        self.assertEqual(talk1.returncode, 0, t1err)
        self.assertIn("Alice", t1out)

        # The bot's next answer arrives on a fresh dial.
        talk2 = self.talk(
            "send", *self.endpoint(info), "--body", "hello Alice"
        )
        self.assertEqual(talk2.returncode, 0, talk2.stderr)
        rc2, out2, err2 = cli2.finish()
        self.assertEqual(rc2, 0, (rc2, out2, err2))
        self.assertIn("[Soul] hello Alice", out2)
        self.close_session(sid)

    def test_close_exits_7(self) -> None:
        sid = "e2e-close"
        cli = self.start_cli("-s", sid, "--wait", "20", "bye")
        info = cli.wait_info()
        talk = self.talk("close", *self.endpoint(info))
        self.assertEqual(talk.returncode, 0, talk.stderr)
        rc, out, err = cli.finish()
        self.assertEqual(rc, 7, (rc, out, err))
        self.assertIn("DELIVERY ack", out)
        self.assertIn("ended (closed)", err)

    def test_ack_only_exits_9(self) -> None:
        sid = "e2e-ack"
        cli = self.start_cli("-s", sid, "--wait", "20", "status please")
        info = cli.wait_info()
        talk = self.talk(
            "send", *self.endpoint(info), "--kind", "status", "--body", "thinking"
        )
        self.assertEqual(talk.returncode, 0, talk.stderr)
        rc, out, err = cli.finish()
        self.assertEqual(rc, 9, (rc, out, err))
        self.assertIn("DELIVERY ack", out)
        self.assertNotIn("thinking", out)  # status is stderr-only
        self.assertIn("# status: thinking", err)
        self.close_session(sid)

    def test_bad_key_is_discarded(self) -> None:
        sid = "e2e-badkey"
        cli = self.start_cli("-s", sid, "--wait", "4", "hello")
        info = cli.wait_info()
        bad_key = "A" * 43 + "="  # 32 zero-ish bytes, wrong for this session
        talk = self.talk(
            "send",
            "--host",
            str(info["host"]),
            "--port",
            str(info["port"]),
            "--key-b64",
            bad_key,
            "--session",
            sid,
            "--from",
            "Soul",
            "--body",
            "should be dropped",
        )
        rc, out, err = cli.finish(timeout=30)
        # The CLI discards the bad frames and exits 8 (nothing arrived).
        self.assertEqual(rc, 8, (rc, out, err, talk.stderr))
        self.assertNotIn("should be dropped", out)
        # The daemon logs discards to the session log, not the caller's stderr.
        logs = list((self.home / "state" / "asksoul" / "sessions").glob("*/log"))
        log_text = "".join(p.read_text(encoding="utf-8") for p in logs)
        self.assertIn("bad MAC", log_text)
        self.close_session(sid)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Config parsing, secret resolution, init and protocol-shape tests."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from support import ASKSOUL, load_asksoul, mode_of, write_private

MOD = load_asksoul("asksoul_config_test")


@contextmanager
def ask_env(**values):
    """Run with only the given ASKSOUL_* variables."""
    old = os.environ.copy()
    for key in list(os.environ):
        if key.startswith("ASKSOUL_"):
            del os.environ[key]
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)


class FlatTomlTests(unittest.TestCase):
    def test_flat_parser(self) -> None:
        text = """
# a comment
bot_name = "Nova"          # trailing comment
webhook_url = 'https://example.test/hook'
accept = ["127.0.0.1", "bot.example"]
enabled = true
n = 3

[transport]
kind = "tcp-tailnet"
"""
        data = MOD._parse_flat_toml(text)
        self.assertEqual(data["bot_name"], "Nova")
        self.assertEqual(data["webhook_url"], "https://example.test/hook")
        self.assertEqual(data["accept"], ["127.0.0.1", "bot.example"])
        self.assertIs(data["enabled"], True)
        self.assertEqual(data["n"], 3)
        self.assertEqual(data["transport.kind"], "tcp-tailnet")

    def test_parse_toml_supports_hash_in_string(self) -> None:
        data = MOD.parse_toml('secret_cmd = "echo a#b"\n')
        self.assertEqual(data["secret_cmd"], "echo a#b")

    def test_parse_toml_falls_back_without_tomllib(self) -> None:
        saved = MOD.tomllib
        MOD.tomllib = None
        try:
            data = MOD.parse_toml(
                'bot_name = "Nova"\naccept = ["127.0.0.1", "bot.example"]\n'
            )
        finally:
            MOD.tomllib = saved
        self.assertEqual(data["bot_name"], "Nova")
        self.assertEqual(data["accept"], ["127.0.0.1", "bot.example"])

    def test_comma_string_accept(self) -> None:
        data = {"accept": "127.0.0.1, 10.0.0.0/8"}
        with ask_env():
            allowed = MOD.accept_list(data)
        self.assertIn("127.0.0.1", allowed)
        self.assertIn("10.0.0.0/8", allowed)


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_env_overrides_file(self) -> None:
        path = write_private(
            self.home / "config.toml",
            'bot_name = "Nova"\nwebhook_url = "https://file.test/hook"\n'
            'secret_file = "%s"\n' % (self.home / "secret"),
        )
        write_private(self.home / "secret", "file-secret\n")
        data = MOD.load_config_data(path)
        with ask_env(
            ASKSOUL_BOT_NAME="EnvBot",
            ASKSOUL_WEBHOOK_URL="https://env.test/hook",
            ASKSOUL_WEBHOOK_SECRET="env-secret",
        ):
            self.assertEqual(MOD.bot_name_from_data(data), "EnvBot")
            self.assertEqual(
                MOD.webhook_url_from_data(data), "https://env.test/hook"
            )
            self.assertEqual(MOD.resolve_secret(data), "env-secret")

    def test_secret_cmd_beats_file(self) -> None:
        secret_file = write_private(self.home / "secret", "file-secret\n")
        data = {
            "secret_file": str(secret_file),
            "secret_cmd": "printf cmd-secret",
        }
        with ask_env():
            self.assertEqual(MOD.resolve_secret(data), "cmd-secret")

    def test_secret_file_requires_0600(self) -> None:
        secret_file = write_private(self.home / "secret", "file-secret\n")
        os.chmod(secret_file, 0o644)
        with ask_env():
            with self.assertRaises(MOD.UsageError):
                MOD.resolve_secret({"secret_file": str(secret_file)})

    def test_missing_secret_is_usage_error(self) -> None:
        with ask_env():
            with self.assertRaises(MOD.UsageError):
                MOD.resolve_secret({})

    def test_bad_webhook_url(self) -> None:
        with ask_env():
            with self.assertRaises(MOD.UsageError):
                MOD.webhook_url_from_data({"webhook_url": "ftp://nope"})

    def test_identity_defaults_and_override(self) -> None:
        with ask_env():
            self.assertEqual(
                MOD.identity_from_data({}), ("this host", "asksoul")
            )
        with ask_env(ASKSOUL_WHO="alice", ASKSOUL_SOURCE="laptop"):
            self.assertEqual(
                MOD.identity_from_data({}), ("alice", "laptop")
            )

    def test_accept_resolution(self) -> None:
        with ask_env():
            allowed = MOD.accept_list(
                {"accept": ["127.0.0.1", "127.0.0.0/8", "localhost"]}
            )
        self.assertIn("127.0.0.1", allowed)
        self.assertIn("127.0.0.0/8", allowed)
        self.assertTrue(MOD.peer_allowed("127.0.0.5", {"127.0.0.0/8"}))
        self.assertFalse(MOD.peer_allowed("10.1.2.3", {"127.0.0.0/8"}))

    def test_accept_ssh_host_is_optional(self) -> None:
        with ask_env():
            with self.assertRaises(MOD.SetupError):
                MOD.accept_list({})

    def test_state_home_follows_xdg(self) -> None:
        with ask_env(XDG_STATE_HOME=str(self.home / "state")):
            self.assertEqual(
                MOD.sessions_root(), self.home / "state" / "asksoul" / "sessions"
            )

    def test_payload_shape(self) -> None:
        payload = MOD.build_payload(
            "hi", {"host": "h"}, "sid", "src", "who", "user-tui"
        )
        self.assertEqual(
            set(payload),
            {
                "source",
                "who",
                "sender_type",
                "intent",
                "message",
                "reply",
                "session_id",
            },
        )
        self.assertEqual(payload["sender_type"], "user-tui")
        self.assertEqual(payload["intent"], "asksoul")
        self.assertEqual(payload["session_id"], "sid")

    def test_reply_object_shape(self) -> None:
        reply = MOD.reply_object("127.0.0.1", 1234, 300, b"k" * 32, "who")
        self.assertEqual(reply["host"], "127.0.0.1")
        self.assertEqual(reply["port"], 1234)
        self.assertEqual(reply["crypto"]["alg"], "hmac-sha256-v1")
        self.assertEqual(reply["accept"], "session")
        self.assertIn("key_b64", reply["crypto"])


class InitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.config = self.home / "cfg" / "config.toml"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_init(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("ASKSOUL_")
        }
        env["HOME"] = str(self.home)
        env["ASKSOUL_CONFIG"] = str(self.config)
        return subprocess.run(
            [str(ASKSOUL), "init", *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

    def test_init_writes_config_and_secret_0600(self) -> None:
        proc = self.run_init(
            "--non-interactive",
            "--webhook-url",
            "https://example.test/hook",
            "--bot-name",
            "Nova",
            "--accept",
            "127.0.0.1,localhost",
            "--secret-stdin",
            stdin="top-secret\n",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.config.is_file())
        self.assertEqual(mode_of(self.config), 0o600)
        self.assertEqual(mode_of(self.config.parent), 0o700)
        secret = self.config.parent / "secret"
        self.assertTrue(secret.is_file())
        self.assertEqual(mode_of(secret), 0o600)
        self.assertEqual(secret.read_text().strip(), "top-secret")

        data = MOD.load_config_data(self.config)
        with ask_env():
            self.assertEqual(MOD.bot_name_from_data(data), "Nova")
            self.assertEqual(
                MOD.webhook_url_from_data(data), "https://example.test/hook"
            )
            self.assertEqual(MOD.resolve_secret(data), "top-secret")
            self.assertIn("127.0.0.1", MOD.accept_list(data))

    def test_init_secret_cmd(self) -> None:
        proc = self.run_init(
            "--non-interactive",
            "--webhook-url",
            "https://example.test/hook",
            "--secret-cmd",
            "printf another",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((self.config.parent / "secret").exists())
        data = MOD.load_config_data(self.config)
        with ask_env():
            self.assertEqual(MOD.resolve_secret(data), "another")

    def test_init_requires_url_non_interactive(self) -> None:
        proc = self.run_init("--non-interactive")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--webhook-url", proc.stderr)
        self.assertFalse(self.config.exists())

    def test_init_refuses_overwrite_without_force(self) -> None:
        args = (
            "--non-interactive",
            "--webhook-url",
            "https://example.test/hook",
        )
        first = self.run_init(*args)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_init(*args)
        self.assertEqual(second.returncode, 1)
        self.assertIn("--force", second.stderr)
        forced = self.run_init(*args, "--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)

    def test_init_help_and_version(self) -> None:
        proc = self.run_init("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--webhook-url", proc.stdout)


if __name__ == "__main__":
    unittest.main()

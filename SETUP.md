# Set up AskSoul

You are the **main bot** and the human gave you this repo. Your job is to
make `asksoul "..."` work on the human's machine. Do the steps marked
**BOT** yourself. Do not skip the human-only steps; ask for them plainly.

The moving parts:

- **Human machine:** the `asksoul` CLI, a config, and a TCP listener.
- **Grok Bot:** one routine with a webhook trigger. That routine runs the
  prompt in [`bot/routine-prompt.md`](bot/routine-prompt.md) and talks back
  with `asksoul-talk`.
- **Your computer:** `asksoul-talk` on `PATH`.

---

## 1. BOT — install `asksoul-talk` on your own computer

```sh
curl -fsSL https://raw.githubusercontent.com/wonder-missing/asksoul/main/install.sh | sh -s -- --bot
```

This puts `asksoul-talk` in `~/bin` (or `~/.local/bin`). Verify:

```sh
command -v asksoul-talk
asksoul-talk --help >/dev/null && echo ok
```

If the directory is not on `PATH`, add it to your shell profile. If your
name is not `Soul`, export it so every frame is signed with your name:

```sh
export ASKSOUL_BOT_NAME='YourName'
```

## 2. HUMAN — create the webhook routine

The human must do this in Grok Bot. You cannot create the trigger for them.

1. Create a **routine with a webhook trigger** on this bot.
2. Paste the entire prompt from [`bot/routine-prompt.md`](bot/routine-prompt.md)
   into the routine.
3. Copy the webhook **URL** and **secret**.

Then the human either keeps the URL/secret for step 4, or hands them to you
**only if you are configuring their machine**. Never put the secret in chat,
in the routine, or in a log.

## 3. BOT — install the CLI on the human's machine

Do this yourself if you have shell access to the human machine. Otherwise
give them this one-liner:

```sh
curl -fsSL https://raw.githubusercontent.com/wonder-missing/asksoul/main/install.sh | sh
```

That installs `~/.local/bin/asksoul`. Make sure `~/.local/bin` is on `PATH`.

## 4. BOT or HUMAN — write the config

Prefer a secret store over a plain file. Run on the **human machine**.

macOS Keychain:

```sh
security add-generic-password -a asksoul -s asksoul -w '<WEBHOOK_SECRET>'
asksoul init \
  --webhook-url '<WEBHOOK_URL>' \
  --bot-name 'YourName' \
  --accept '<BOT_IP_OR_HOST>' \
  --secret-cmd 'security find-generic-password -s asksoul -w'
```

Linux with a secret file (written mode 0600 by `init`):

```sh
printf '%s\n' '<WEBHOOK_SECRET>' | asksoul init \
  --webhook-url '<WEBHOOK_URL>' \
  --bot-name 'YourName' \
  --accept '<BOT_IP_OR_HOST>' \
  --secret-stdin
```

Or just run `asksoul init` and answer the prompts. Secret precedence at
runtime: `ASKSOUL_WEBHOOK_SECRET` > `secret_cmd` > `secret_file`.

Config lives at `~/.config/asksoul/config.toml` (mode 0600). `accept` is the
allowlist described in the next step.

## 5. HUMAN or BOT — make the network reachable

The **bot's computer must open a TCP connection to the human machine** on the
ephemeral port in each webhook. Use [Tailscale](https://tailscale.com) on
both machines; the CLI binds the tailnet IPv4 by default.

- With Tailscale, set `accept` to the bot computer's Tailscale IP, its
  MagicDNS hostname, or the tailnet CIDR. Hostnames are resolved when the
  session opens.
- If Tailscale is not in use, set `bind` to the human machine's private
  address and put the bot computer's address in `accept`.
- Allow inbound TCP on that interface in the human machine's firewall.
- `accept_ssh_host = "<ssh-config-host>"` also resolves one peer through
  `ssh -G <host>`.
- `accept` is only an allowlist: the HMAC key in each webhook is the real
  authentication. Never expose the listener to the public internet and never
  log the key or the webhook secret.

Check from the bot computer:

```sh
# after a session is open, the CLI prints "listening HOST:PORT" on stderr
nc -vz <HOST> <PORT>
```

## 6. Smoke test

On the human machine:

```sh
asksoul --wait 180 "hello, this is a setup test"
```

Expect a `# ack (by YourName)` line, a `[YourName] ...` answer, and
`DELIVERY ack`; the exit code should be 0. A cold start can take a minute or
more, so wait in minutes, not seconds.

Then the interactive check:

```sh
asksoul tui
```

Type a line, press Enter, get an answer, then `/quit`. The TUI sends
`sender_type = "user-tui"`: the bot must answer with `ask` and must not close
the session after each turn.

If both pass, setup is done.

---

## Troubleshooting by exit code

| Exit | What it means | What to do |
|---|---|---|
| 0 | Answer received; session still open | Close it with `asksoul close <ID>` when done. |
| 1 | Usage error, unknown session, or Ctrl-C | Check the command and `-s <ID>`; read the stderr line. |
| 2 | Webhook failed after 3 attempts; nothing was kept | Check the webhook URL, the secret, and network access from the human machine to Grok Bot. |
| 3 | Session ended on `-t` silence | The routine did not answer. Check the routine run log; retry with `--wait 180` / `-t 600`. |
| 4 | Setup error | Check `bind`, `accept`, Tailscale, and that `python3` is 3.9+. |
| 5 | Three bad MACs | The bot is using an old key/port. Start a new session and pass the newest `key_b64`. |
| 6 | Relayed to a teammate bot | Success; do **not** resend. `asksoul wait <ID>` may still read a later line. |
| 7 | The bot closed the conversation | Nothing to fix. Start a new session to keep talking. |
| 8 | `--wait` ran out with nothing yet | `asksoul wait <ID>` keeps waiting; do not resend. |
| 9 | Only an `ack` arrived | `asksoul wait <ID>` keeps waiting; do not resend. |

Bot-side failures:

| `asksoul-talk` exit | What it means | What to do |
|---|---|---|
| 1 | Dial/send failed and nothing could be queued (`status`/`close`) | Check that the human machine is up and the accept list/firewall allows you; retry with the same triple. |
| 4 | `msg`/`relayed` queued in the outbox | Same connectivity check. The next successful dial to that session flushes the queue. |
| 7 | The CLI sent `close` | Stop; the conversation is over. |
| 3 | `ask` timed out | The caller did not answer in time; the question was sent. Decide whether to `ask` again or `close`. |

Never resend after exit 3, 6, 8 or 9 on the CLI side, or after exit 4 on the
bot side without first making a successful dial.

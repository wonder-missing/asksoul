# AskSoul protocol

AskSoul has two transports:

1. **Webhook (CLI → platform).** One JSON POST wakes the bot routine.
2. **TCP (bot → CLI).** Line-delimited, HMAC-signed JSON frames carry acks,
   text, relay receipts and close.

All examples use `Soul` as the bot name; any configured `bot_name` works the
same way.

## 1. Webhook payload

`POST <webhook_url>` with `Content-Type: application/json` and
`Authorization: Bearer <secret>` (a value that already starts with `Bearer `
is sent as-is). A new JSON body per call:

```json
{
  "source": "asksoul",
  "who": "alice-laptop",
  "sender_type": "user-cli",
  "intent": "asksoul",
  "message": "Can you ask the designer bot for a logo?",
  "session_id": "7f3c2a54-...",
  "reply": {
    "required": true,
    "mode": "session",
    "transport": "tcp-tailnet",
    "host": "192.0.2.10",
    "port": 51234,
    "timeout_sec": 300,
    "crypto": {
      "alg": "hmac-sha256-v1",
      "key_b64": "<base64 of 32 random bytes>"
    },
    "accept": "session",
    "note": "<human-readable protocol summary>"
  }
}
```

- `sender_type` is `user-cli` for a one-shot ask and `user-tui` for
  `asksoul tui` (a continuous conversation).
- `session_id` names the thread. It is chosen by `-s` or generated as a UUID.
- `host` + `port` + `crypto.key_b64` are **one atomic triple**. A new
  webhook port voids every earlier key, even under the same `session_id`.
- Retries: up to 3 attempts, backing off 2 s then 5 s, on network errors,
  HTTP 429 and HTTP 5xx. Other HTTP statuses fail immediately. After the
  third failure the CLI exits 2 and keeps nothing.
- `host` is the local address the CLI bound (`bind`, or `tailscale ip -4`).
  `port` is ephemeral.

## 2. TCP listener

The CLI binds `host:port` once per session, in a background process. Rules:

- Only peers on the **accept allowlist** may connect: literal IPv4s,
  hostnames (resolved to IPv4) and CIDRs from config `accept` /
  `ASKSOUL_ACCEPT`, plus an optional `accept_ssh_host` resolved through
  `ssh -G`.
- One active bot connection at a time. A sequential reconnect is fine. A
  second concurrent writer is rejected. A new idle peer replaces an idle
  previous one.
- Disconnecting without `close` does **not** end the session; the listener
  keeps waiting on the same host:port.
- The socket is authoritative for one turn; the session process owns it.

## 3. Frames

Each frame is one JSON object, UTF-8, followed by `\n`, flushed:

```json
{
  "v": 1,
  "alg": "hmac-sha256-v1",
  "kind": "msg",
  "session_id": "7f3c2a54-...",
  "from": "Soul",
  "mac_b64": "<base64 HMAC>",
  "body_b64": "<base64 UTF-8 body>"
}
```

`v`, `alg`, `kind`, `session_id`, `mac_b64` and `body_b64` are required.
`from` and `queued_at` are optional. A missing or mismatched `session_id`,
unknown `alg` or `v`, unknown `kind`, invalid base64, or an oversized frame
(> 1 MiB) is discarded; other discards keep the session alive.

### HMAC

`key` is the 32 bytes in `reply.crypto.key_b64`; it lives only in memory and
in that webhook payload. HMAC-SHA256 over the raw body bytes with one of
three canonical inputs, depending on the frame:

| Frame carries | MAC input (bytes) |
|---|---|
| no `from`, no `queued_at` | `body` |
| `from`, no `queued_at` | `from_utf8 + b"\n" + body` |
| `from` + `queued_at` | `from_utf8 + b"\0" + queued_at + b"\n" + body` |

`queued_at` is valid only on `msg` and `relayed`, and only together with
`from`. It is ISO 8601 with seconds and an explicit offset, e.g.
`2026-10-09T21:15:49+08:00` or `...49Z`.

`from` is 1–64 characters, not all whitespace, no control characters and no
`[` or `]`. A bad `from` or `queued_at` discards the frame.

The CLI compares with `hmac.compare_digest`. Three consecutive bad MACs end
the session (CLI exit 5). The first one also logs a stale-crypto hint.

## 4. Kinds

| kind | Sender | Body | CLI effect |
|---|---|---|---|
| `ack` | bot | empty | `# ack (by <from>)`; the dialer received the latest line(s). Sent automatically as the first frame on every dial. |
| `status` | bot | short state | printed to stderr as `# status: ...`; does not end a turn |
| `msg` | either | UTF-8 text | bot → `[<from>] text` on stdout; CLI → `msg` frame to the bot |
| `relayed` | bot | target bot name | `# relayed: <name> (by <from>)`, turn exits 6; session stays up |
| `close` | either | optional reason | ends the CLI session (exit 7 for a bot close) |

The CLI's own outbound `msg`/`close` frames carry no `from` and no
`queued_at`, so their MAC is over `body`.

Convention: senders end `msg` bodies with `\n`. The CLI does not add or strip
inbound newlines; `asksoul-talk` appends the trailing newline on outbound
`msg` frames.

## 5. Session lifecycle

1. `asksoul "text"` binds the listener, creates a key, POSTs the webhook, and
   starts the background session.
2. The bot dials back with the webhook's triple and sends `ack` first.
3. It sends `msg` (an answer), optionally `relayed` (hand-off), or continues
   with another `ask` turn.
4. The caller may send more lines with `-s ID`. If no bot is connected, the
   session re-POSTs a webhook for the same `session_id` and the bot dials in
   again.
5. The session ends on `close`, on `-t` seconds of silence, or on a bad-MAC
   abort. Unread frames are kept for `wait ID` for up to another `-t`.

A turn ends 2 s after the last `msg`/`relayed`/`ack`, when the bot closes, or
when `--wait` expires. Anything later is buffered for the next `wait`/`-s`
call.

## 6. CLI exit codes

| Code | Meaning |
|---|---|
| 0 | got an answer (session still open), or `close` done |
| 1 | usage error, unknown session, or Ctrl-C |
| 2 | webhook unreachable; message not delivered and not kept |
| 3 | session ended by the `-t` silence timeout |
| 4 | setup error (config, bind, allowlist, crypto) |
| 5 | ended after 3 consecutive bad MACs; start a new session |
| 6 | relayed to a teammate bot; done, do not resend |
| 7 | the bot closed the conversation |
| 8 | `--wait` ran out with nothing yet; `wait ID` keeps waiting |
| 9 | only an `ack` arrived; `wait ID` keeps waiting |

## 7. Bot helper exit codes (`asksoul-talk`)

| Code | Meaning |
|---|---|
| 0 | frames sent (or, for `ask`, the caller's next line arrived) |
| 1 | dial/send failed and nothing was queueable (`status`/`close`) |
| 3 | `ask` wait timed out; the line was sent |
| 4 | msg/relayed frames were queued in the outbox after a dial/write failure |
| 7 | the CLI sent `close` (session over) |

`asksoul-talk` sends an automatic `ack` on every dial, then flushes stored
outbox frames for the session, then this call's frames. An outbox row is
deleted only after a successful write. `status` and `close` are never queued.
The outbox lives under `$XDG_STATE_HOME/asksoul-talk/outbox.sqlite3`
(default `~/.local/state/asksoul-talk/outbox.sqlite3`), mode 0600 in a 0700
directory, and holds no keys, hosts or ports.

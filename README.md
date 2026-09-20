# free-astra

Use **GPT-6-Astra** from your own local Codex, for free.

It comes from **prism.openai.com**, OpenAI's online LaTeX editor, whose AI panel
runs the frontier models. No paid plan, no API key, no credit card: sign in to
Prism in your browser and free-astra bridges that session into Codex.

```bash
git clone https://github.com/Zhao73/free-astra && cd free-astra
./install.sh
```

Then pick a model like any other:

```bash
codex exec --model prism-astra "fix the failing test"
codex --model prism-sol
```

Your normal models keep working in the same session, same picker, same CLI, and in
the Codex desktop app. Nothing is replaced.

## What you get

| Model | Backed by |
|---|---|
| `prism-astra` | GPT-6-Astra via Prism |
| `prism-sol` | GPT-5.6-Sol via Prism |
| `prism-terra` | GPT-5.6-Terra via Prism |

Prism's allowlist is not ours and it changes without notice: `gpt-6-astra` was
accepted in the morning of 2026-09-17 and rejected by the afternoon with
`400: Unsupported assistant model`. Model identity is strict by default: a request
for `prism-astra` now fails if Astra is unavailable instead of silently returning a
different model. For the old local-Codex behaviour, explicitly set
`PRISM_ALLOW_MODEL_FALLBACK=1`; do not enable that for metered/public API service.

It is a real agent, not a chat box: it runs commands and edits files on your
machine. Verified by handing it a Python file with two bugs, which it fixed and
re-ran, output confirmed on disk.

**Honest notes.** If your ChatGPT plan already gives you these models in Codex,
use them natively - you get real streaming and real token accounting, and this
adapter gives you neither. free-astra is for everyone else: Prism asks for a
signed-in browser session and nothing more, so the models are reachable without a
paid plan. Prism still applies its own daily and monthly usage limits, and those
are separate from your Codex ones.

## Requirements

- A ChatGPT account signed in at prism.openai.com. A free one is enough - Prism
  reports `entitlements: not_required`.
- `codex` CLI, signed in (`codex login`). Your Codex catalog does not need to
  contain Astra; free-astra clones whichever model you do have as a template.
- Python 3.9+, and `pip install zstandard` (Codex compresses request bodies)
- macOS or Linux. The keep-alive service is macOS-only; elsewhere run `freeastra.py`
  yourself.

## How it works

Prism is not an API. Its AI backend is a Codex agent in a remote sandbox, driven by
a start-then-poll pair of endpoints and authenticated with your browser cookies:

```
POST /api/backend/1/new                   -> {url, token}   mint a sandbox
POST /api/llm/response_with_tools_start    -> {request_id, turn_state}
POST /api/llm/response_with_tools_status   -> poll until completed
```

free-astra is a single-file adapter that speaks the OpenAI Responses API to Codex
and that protocol to Prism. It sits in front of Codex as the one front door and
splits by model name: `prism-*` it answers itself, everything else it forwards
untouched to whatever was upstream before — the real Codex backend, or a gateway
like opencodex if you already had one.

### The four things that make it work

Each of these was a dead end first, so they are worth stating plainly.

1. **The whole conversation goes in one message.** Prism keeps only the last user
   message; prior turns in `input` are dropped, and reusing `conversationId` does
   not bring them back.
2. **The model must be told it is not the one acting.** Prism's model is itself an
   agent with its own sandbox. Asked to "create a file", it creates one *there* and
   truthfully reports success while your disk stays untouched. Framed instead as a
   component that only emits the next JSON action and never executes anything, it
   emits clean tool calls.
3. **Codex's own instructions have to go.** Codex sends ~60 KB of developer
   instructions asserting the model owns a shell. Left in, they beat the bridge
   protocol. They are stripped by default (`PRISM_KEEP_INSTRUCTIONS=1` keeps them).
4. **The sandbox token must be warm.** A freshly minted sandbox cold-boots for
   minutes and then 504s. Setup lifts the token from a real request you already
   made, which points at a sandbox that is already up.

### Why the model slugs are renamed

Codex reads each model's tool behaviour from its catalog. For an official slug it
prefers its own cached metadata, which says `code_mode_only` — a mode whose entry
point is a freeform tool taking raw JavaScript. Under the `prism-*` slugs it uses
the metadata free-astra supplies instead, and sends ordinary function tools that
can be emulated.

## Commands

```bash
./install.sh                 # first run: session, model list, wiring
./install.sh --resession     # replace an expired session
./front-door.sh status       # what is wired up right now
./front-door.sh off          # unwire, restore the previous upstream
./front-door.sh unservice    # remove the keep-alive service
python3 freeastra.py --demo  # offline self-check
```

Effort comes from `PRISM_EFFORT` (`low`/`medium`/`high`) or a `model` suffix like
`prism-astra:high`.

## API / gateway mode

The default remains the original local Codex front-door behaviour. For a dedicated
upstream behind NewAPI or another gateway, run in fail-closed API mode:

```bash
PRISM_API_ONLY=1 \
PRISM_API_KEY='replace-with-an-internal-secret' \
PRISM_BIND=127.0.0.1 \
python3 freeastra.py
```

Point the gateway at `http://127.0.0.1:8319/v1` (or a private network address if
the adapter runs on another host). API mode only exposes the `prism-*` model
aliases through `/v1/models`; unknown models and routes are rejected instead of
being passed through to the Codex backend, so caller Authorization headers cannot
leak across that boundary. API-only requests must use normal Content-Length framing
(no `Transfer-Encoding: chunked`), and must provide an explicit non-null `model`.
Responses requests must include `input`; Chat Completions requests must include
`messages`. Without `PRISM_API_KEY`, API mode only accepts a loopback `PRISM_BIND`;
it refuses to start on a non-loopback address unless `PRISM_ALLOW_INSECURE=1`.

For long-running HTTP requests, streaming connections are opened before Prism
finishes and receive SSE heartbeat comments while Prism is polling. Responses API
streams emit the normal text/function argument lifecycle events. Unsupported
features such as `previous_response_id`, stored/background responses, image/file
input, structured output and parallel tool calls fail explicitly instead of being
silently approximated. Disconnects are noticed for both streaming and non-streaming
requests; the abandoned Prism turn is cancelled so the single sandbox is released
at the next upstream stage instead of running to the full timeout.

Useful service settings:

- `PRISM_QUEUE_TIMEOUT` — maximum wait for the single sandbox slot (default 15s).
- `PRISM_SSE_HEARTBEAT` — seconds between SSE heartbeat comments (default 10s).
- `PRISM_MAX_BODY` — maximum HTTP request body size. In `PRISM_API_ONLY=1`,
  compressed request bodies are rejected before decompression to avoid decompression
  bombs; keep compression disabled at this adapter boundary.
- `PRISM_MAX_TOOL_SCHEMA` — maximum JSON size of one emulated tool schema.
- `PRISM_API_KEY` — optional bearer token required by adapter API routes.
- `PRISM_ALLOW_INSECURE` — allow API-only serving without a key on a non-loopback
  bind; do not set this on an untrusted network.
- `PRISM_API_REFRESH` — API-only mode disables the bundled browser session refresh
  by default; run an external session/account refresher, or set this to `1` to let
  the adapter run `refresh-session.sh` itself.
- `PRISM_KEEPALIVE` — seconds between keep-alive pings; defaults to `0` in API-only
  mode (and `600` otherwise). Set a positive value to keep a sandbox warm.
- `PRISM_BIND` — bind address (IPv4, hostname, or IPv6 such as `::1`); defaults to
  loopback.

`/healthz` is a liveness check; `/readyz` reports the captured session fields only
and does not probe Prism, so it cannot detect a stale cookie or a cold sandbox.
HEAD mirrors the adapter's own routes with an empty body; in the default front-door
mode it does not pass unknown routes upstream or merge the upstream model catalog,
so use GET where that matters.

The adapter still cannot provide authoritative token usage: Prism does not expose
it, so `usage` remains zero. Do not use upstream usage for billing. A single Prism
sandbox also remains effectively single-flight; scale with isolated account/sandbox
workers rather than increasing `PRISM_CONCURRENCY` on one sandbox.

### Scaling with multiple accounts

One Prism account owns one sandbox and runs a single turn at a time. To raise
concurrency, give each account its own adapter instance and register every instance
as a separate gateway channel with the same `prism-*` models:

```bash
mkdir -p ~/.free-astra/accounts
# capture each account's session as ~/.free-astra/accounts/<name>.json
PRISM_ACCOUNTS_DIR=~/.free-astra/accounts PRISM_API_KEY=... scripts/account-pool.sh start
scripts/account-pool.sh ports   # name -> port map, for gateway channel setup
scripts/account-pool.sh status
scripts/account-pool.sh stop
```

Instances listen on consecutive ports starting at `PRISM_POOL_BASE_PORT` (default
8319), with one log per account under the state directory. Ports are persisted per
account, so adding or removing one account does not move the ports the gateway is
already configured for. A busy instance answers before the stream starts with HTTP
503 `prism_busy`, so a gateway that retries 5xx (NewAPI retries 500-503 by default)
spills the request to the next channel instead of failing it.

Timeouts: keep `PRISM_TIMEOUT` (per turn, default 240s) below the gateway's
streaming timeout (NewAPI `STREAMING_TIMEOUT`, default 300) and first-byte timeout
(`RELAY_RESPONSE_HEADER_TIMEOUT`, default 1800). `PRISM_QUEUE_TIMEOUT` (default 15)
is how long a request waits on one instance before it reports `prism_busy`.

Billing: Prism exposes no token usage, so responses report `usage: 0` and a
token-priced model would cost nothing. If the gateway must meter these models,
assign them a fixed per-call price instead (NewAPI model price settings), for
example `{"prism-astra": 0.01, "prism-sol": 0.01, "prism-terra": 0.01}`.

## Sessions expire

Cookies last about 12 hours and the sandbox goes cold sooner. free-astra notices and
re-captures by itself when it can, which needs the optional
[chrome-use](https://github.com/leeguooooo/chrome-use) CLI and a Prism project URL
in your session file. Otherwise it fails fast with:

```
... Run ./scripts/refresh-session.sh and retry.
```

and `./install.sh --resession` takes 30 seconds. Set `PRISM_NO_AUTO_REFRESH=1` to
never let it drive your browser.

## One thing we ask

When the install finishes and the smoke test passes, free-astra asks once whether
you want to star the repo. That is the only prompt it will ever show you: it writes
a marker file, so it never asks twice, and it stays silent when there is no terminal
to ask (CI, pipes, the background service). Nothing is starred without you typing
`y`. To skip it entirely:

```bash
FREE_ASTRA_NO_PROMPT=1 ./install.sh
```

If it saved you something, a star genuinely helps other people find it.

## Fair warning

This drives an OpenAI product through an interface that was not published for it.
It needs *your* account and shares nothing between users, but it is unsupported, it
can break whenever Prism changes, and it may well be against the terms you agreed
to. Accounts have been restricted for less. Your call.

No credentials leave your machine. `~/.free-astra/session.json` holds your cookies
and is chmod 600; it is gitignored and nothing uploads it.

## License

MIT

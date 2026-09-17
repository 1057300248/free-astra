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

#!/usr/bin/env python3
"""free-astra - use your own prism.openai.com models from local Codex.

Prism is not an OpenAI API. It is a start+poll agent runtime:
  POST /api/llm/response_with_tools_start   -> {request_id, turn_state, conversation_id}
  POST /api/llm/response_with_tools_status  -> poll until status == completed
Auth is the logged-in browser session (cookies). It needs a warm sandbox token.

Two behaviours the upstream forces on us, both handled here:
  * prior turns in `input` are DROPPED by the server - only the last user message
    survives. So the whole conversation is flattened into one user message.
  * caller-supplied `tools` are IGNORED - the remote model runs its own sandbox
    tools instead. So tool-calling is emulated in the prompt and parsed back out.

Stdlib only, single file. See README.md for how the pieces fit.
"""
import hmac, json, os, re, sys, time, threading, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = "https://prism.openai.com"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("FREE_ASTRA_HOME", os.path.expanduser("~/.free-astra"))
SESSION_FILE = os.environ.get("PRISM_SESSION", os.path.join(STATE, "session.json"))
MANIFEST = os.path.join(STATE, "models.json")
PORT = int(os.environ.get("PRISM_PORT", "8319"))
MODELS = ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra"]
# custom slugs exist only so Codex has no official ModelInfo to prefer over ours
ALIAS = {"prism-astra": "gpt-6-astra", "prism-sol": "gpt-5.6-sol", "prism-terra": "gpt-5.6-terra"}
DEFAULT_EFFORT = os.environ.get("PRISM_EFFORT", "medium")
# Total budget for one answer. Without this a cold sandbox makes the adapter block
# for minutes and Codex just sits on "thinking" with nothing to show the user.
BUDGET = float(os.environ.get("PRISM_TIMEOUT", "240"))
# Tool output piles up fast (a skills scan alone ran to 80 KB) and a prompt that
# size makes Prism time out. Keep the newest history and elide the rest.
MAX_TRANSCRIPT = int(os.environ.get("PRISM_MAX_TRANSCRIPT", "24000"))
MAX_TOOL_SCHEMA = int(os.environ.get("PRISM_MAX_TOOL_SCHEMA", "12000"))
QUEUE_TIMEOUT = float(os.environ.get("PRISM_QUEUE_TIMEOUT", "15"))
SSE_HEARTBEAT = float(os.environ.get("PRISM_SSE_HEARTBEAT", "10"))
MAX_BODY = int(os.environ.get("PRISM_MAX_BODY", str(4 * 1024 * 1024)))
BIND = os.environ.get("PRISM_BIND", "127.0.0.1")
API_KEY = os.environ.get("PRISM_API_KEY", "")
API_ONLY = os.environ.get("PRISM_API_ONLY", "").lower() in ("1", "true", "yes", "on")
ALLOW_FALLBACK = os.environ.get("PRISM_ALLOW_MODEL_FALLBACK", "").lower() in ("1", "true", "yes", "on")
KEEP_INSTRUCTIONS = os.environ.get("PRISM_KEEP_INSTRUCTIONS", "").lower() in ("1", "true", "yes", "on")
CHATGPT_UPSTREAM = "https://chatgpt.com/backend-api/codex"
OPENCODEX_UPSTREAM = "http://127.0.0.1:10100/v1"


def pick_upstream():
    """Sit in front of opencodex when it is running, so its providers keep working.
    Otherwise talk to the real Codex backend directly."""
    forced = os.environ.get("PRISM_UPSTREAM") or _session.get("upstream")
    if forced:
        return forced
    try:
        with urllib.request.urlopen("http://127.0.0.1:10100/healthz", timeout=1.5) as f:
            if f.status == 200:
                return OPENCODEX_UPSTREAM
    except Exception:
        pass
    return CHATGPT_UPSTREAM


UPSTREAM = CHATGPT_UPSTREAM
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

TOOL_PROTOCOL = """<role>next-action emitter</role>

You are ONE COMPONENT IN A PIPELINE. A separate executor process runs commands on
the user's machine. You do not. You never execute anything, you never touch a
filesystem, and you never report work as done from your own knowledge.

Your entire contract: read the task plus the transcript of what the executor has
already run, then emit ONE JSON action. The executor runs it and appends the result
to the transcript, then asks you again. Prose output breaks the pipeline and is
discarded, so never explain, never apologise, never use markdown fences.

Available actions:
%s

Shell guidance:
- `apply_patch` is NOT a shell command. Never pipe into it, never call it from a
  shell. Use it only if it appears as an action in the list above.
- Do NOT use heredocs (<<EOF). The executor's shell often cannot create the temp
  file they need.
- To write a file, redirect printf:
    printf '%%s\\n' 'first line' 'second line' > path/to/file
- To read a file use `cat`, to search use `rg`. Verify with `cat` after writing.
- A command failing does not mean the workspace is read-only. Diagnose the actual
  error text before concluding anything about permissions.
- File paths in arguments are paths on the EXECUTOR's machine, shown above. Never
  guess one, and never pass a path you have not seen in the transcript.
- When a schema says two parameters are mutually exclusive, send only one.

Emit exactly one of:
  {"tool_call":{"name":"<action>","arguments":{...}}}
  {"done":"<one line summary>"}     <- ONLY when the TRANSCRIPT already proves the
                                       task is complete. An empty transcript never
                                       proves anything."""

TOOL_REMINDER = """

================================================================================
Emit ONE JSON action now. Actions: %s
{"tool_call":{"name":"...","arguments":{...}}}  or  {"done":"..."}
Nothing else. Start your reply with { .
================================================================================"""

AGENT_PREAMBLE = ""

# Codex puts the working directory in <cwd>/<filesystem> tags, inside the same
# user message as the plugin list we drop as noise - so pull these out first.
ENV_BLOCK = re.compile(
    r"<(environment_context|cwd|filesystem)>.*?</\1>", re.S)
CWD_LINE = re.compile(r"^.{0,40}(cwd|current working directory)\s*[:=].*$",
                      re.I | re.M)
NOISE = re.compile(r"<(recommended_plugins|plugin_instructions|skills_instructions|"
                   r"apps_instructions)\b")

_lock = threading.Lock()
_refresh_lock = threading.Lock()
# One Prism sandbox runs one Codex turn at a time. Codex happily fires several
# requests at once (the turn, a title, a summary), and the extra ones come back
# 400. Serialise them instead of letting them collide.
_turn_lock = threading.Semaphore(int(os.environ.get("PRISM_CONCURRENCY", "1")))
# Prism's allowlist changes without warning - gpt-6-astra was dropped mid-session
# on 2026-09-17. Fall back rather than failing a running task.
FALLBACKS = {"gpt-6-astra": ["gpt-5.6-sol", "gpt-5.6-terra"],
             "gpt-5.6-sol": ["gpt-5.6-terra", "gpt-6-astra"],
             "gpt-5.6-terra": ["gpt-5.6-sol", "gpt-6-astra"]}
_substitute = {}          # requested model -> one Prism still accepts


class ClientInputError(ValueError):
    pass


class BusyError(RuntimeError):
    pass


class ToolProtocolError(RuntimeError):
    pass


def unsupported(reason):
    return "unsupported assistant model" in (reason or "").lower()
_last_refresh = [0.0]


def try_refresh(reason):
    """Stale cookies or a dead sandbox make every call fail. Re-run the capture
    script once rather than making the user notice and do it by hand."""
    if os.environ.get("PRISM_NO_AUTO_REFRESH"):
        return False
    script = os.path.join(HERE, "scripts", "refresh-session.sh")
    if not os.path.exists(script):
        return False
    waited = _refresh_lock.locked()
    with _refresh_lock:
        if waited and time.time() - _last_refresh[0] < 180:
            return True                # someone just refreshed while we queued; use it
        if time.time() - _last_refresh[0] < 30:
            return False               # genuinely just tried and it did not help
        _last_refresh[0] = time.time()
        sys.stderr.write("[free-astra %s] " % time.strftime("%H:%M:%S") + "session looks stale (%s) - refreshing\n" % reason)
        try:
            import subprocess
            r = subprocess.run(["bash", script], capture_output=True, timeout=300,
                               env=dict(os.environ, PRISM_SESSION=SESSION_FILE))
            if r.returncode != 0:
                sys.stderr.write("[free-astra %s] " % time.strftime("%H:%M:%S") + "refresh failed: %s\n"
                                 % (r.stderr or b"")[-300:].decode("utf-8", "replace"))
                return False
        except Exception as e:
            sys.stderr.write("[free-astra %s] " % time.strftime("%H:%M:%S") + "refresh error: %s\n" % e)
            return False
        load_session()
        sys.stderr.write("[free-astra %s] " % time.strftime("%H:%M:%S") + "session refreshed\n")
        return True
_session = {"cookie": "", "sandbox_url": "", "sandbox_token": "",
            "project_id": None, "user_id": None, "upstream": None}


def load_session():
    with open(SESSION_FILE, encoding="utf-8") as f:
        d = json.load(f)
    _session.update({k: d.get(k, _session.get(k)) for k in _session})
    if not _session["cookie"] or not _session["sandbox_token"]:
        raise SystemExit("%s needs at least 'cookie' and 'sandbox_token' - run "
                         "./install.sh (or scripts/refresh-session.sh)" % SESSION_FILE)
    return _session


def post(path, body, timeout=180):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), method="POST",
        headers={"content-type": "application/json", "cookie": _session["cookie"],
                 "user-agent": UA, "origin": BASE, "referer": BASE + "/", "accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as f:
            return f.status, json.loads(f.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"_error": e.read().decode()[:500]}


def mint_sandbox():
    """Ask Prism for a fresh sandbox. Cold ones need warming, caller retries."""
    st, d = post("/api/backend/1/new", {}, timeout=60)
    if st == 200 and d.get("token"):
        with _lock:
            _session["sandbox_url"] = d["url"].rstrip("/") + "/"
            _session["sandbox_token"] = d["token"]
        return True
    return False


def decompress(body, enc):
    if "gzip" in enc:
        import gzip
        return gzip.decompress(body)
    if "deflate" in enc:
        import zlib
        return zlib.decompress(body)
    if "zstd" in enc:
        try:
            from compression import zstd as _z          # py3.14+
            return _z.decompress(body)
        except ImportError:
            pass
        try:
            import zstandard                            # pip install zstandard
            # Codex omits the content size from the frame header, so the
            # one-shot API refuses it; the streaming object handles it.
            return zstandard.ZstdDecompressor().decompressobj().decompress(body)
        except ImportError:
            raise RuntimeError(
                "request is zstd-compressed; run `pip install zstandard`, or start "
                "Codex with -c features.enable_request_compression=false")
    return body


def upstream_models():
    """Real Codex catalog, so the picker keeps every native model."""
    d = json.load(open(os.path.expanduser("~/.codex/auth.json"), encoding="utf-8"))
    tok = d["tokens"]["access_token"]
    acct = d["tokens"].get("account_id") or ""
    req = urllib.request.Request(
            UPSTREAM + "/models?client_version=0.154.0",
        headers={"authorization": "Bearer " + tok, "chatgpt-account-id": acct,
                 "accept": "application/json", "originator": "codex_cli_rs",
                 "user-agent": "codex-cli/0.154.0"})
    with urllib.request.urlopen(req, timeout=30) as f:
        return json.loads(f.read().decode())


def additional_tool_specs(items):
    """Extract emulatable functions from Codex additional_tools items."""
    out = []
    for it in items or []:
        if not isinstance(it, dict) or it.get("type") != "additional_tools":
            continue

        def walk(ts, ns=None):
            if ts is None:
                return
            if not isinstance(ts, list):
                raise ClientInputError("additional_tools.tools must be an array")
            for t in ts:
                if not isinstance(t, dict):
                    raise ClientInputError("additional_tools must contain objects")
                if t.get("type") == "namespace":
                    name = t.get("name")
                    if name is not None and not isinstance(name, str):
                        raise ClientInputError("tool namespace name must be a string")
                    walk(t.get("tools"), name)
                    continue
                if t.get("type") != "function":
                    continue
                name = t.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise ClientInputError("tool name must be a non-empty string")
                if ns and ns.startswith("mcp__"):
                    continue
                if "parameters" in t:
                    params = t["parameters"]
                elif "input_schema" in t:
                    params = t["input_schema"]
                else:
                    params = {}
                if not isinstance(params, dict):
                    raise ClientInputError("tool parameters for %s must be an object" % name)
                desc = t["description"] if "description" in t else ""
                if not isinstance(desc, str):
                    raise ClientInputError("tool description for %s must be a string" % name)
                out.append((ns, name, params, desc.strip().split("\n")[0][:160]))

        walk(it.get("tools"))
    return out


def tool_specs(tools):
    """Keep the tools we can actually emulate: plain functions, no namespaces."""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            raise ClientInputError("tools must contain objects")
        if t.get("type") in ("namespace", "web_search"):
            continue
        f = t["function"] if "function" in t else t
        if not isinstance(f, dict) or not f:
            raise ClientInputError("tool function must be a non-empty object")
        name = f.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ClientInputError("tool name must be a non-empty string")
        if "parameters" in f:
            params = f["parameters"]
        elif "input_schema" in f:
            params = f["input_schema"]
        else:
            params = {}
        if not isinstance(params, dict):
            raise ClientInputError("tool parameters for %s must be an object" % name)
        encoded = json.dumps(params, ensure_ascii=False)
        if len(encoded) > MAX_TOOL_SCHEMA:
            raise ClientInputError(
                "tool schema for %s is %d bytes; max is %d" %
                (name, len(encoded), MAX_TOOL_SCHEMA))
        desc = f["description"] if "description" in f else ""
        if not isinstance(desc, str):
            raise ClientInputError("tool description for %s must be a string" % name)
        out.append((name, params, desc.strip().split("\n\n")[0][:1000]))
    return out


def env_context(parts):
    """Just the shell environment, nothing else.

    Matching loosely on "cwd" used to pull in 4 KB of whatever message happened to
    contain the word - desktop-app context, memory from other projects - and the
    model would happily go work on that instead of the task.
    """
    out = []
    for c in parts:
        if not c:
            continue
        out += [m.group(0) for m in ENV_BLOCK.finditer(c)]
    if not out:
        for c in parts:
            out += [m.group(0).strip() for m in CWD_LINE.finditer(c or "")][:3]
    seen, uniq = set(), []
    for o in out:
        if o not in seen:
            seen.add(o)
            uniq.append(o)
    return "\n".join(uniq)[:1500]


def clamp_transcript(entries, budget=None):
    """Keep the most recent entries that fit, oldest first, noting what was cut."""
    budget = MAX_TRANSCRIPT if budget is None else budget
    if not entries:
        return []
    kept, total = [], 0
    for e in reversed(entries):
        if total + len(e) > budget:
            break
        kept.append(e)
        total += len(e)
    kept.reverse()
    if not kept:                        # a single entry larger than the whole budget
        return ["...(truncated)...\n" + entries[-1][-budget:]]
    dropped = len(entries) - len(kept)
    if dropped:
        kept.insert(0, "...(%d earlier steps omitted)..." % dropped)
    return kept


def assemble(sys_parts, convo, tools, env_parts=None):
    """Fold everything into one system + one user message.

    Upstream keeps only the last user message, so the transcript has to travel
    inside it. With tools present we switch to the next-action-emitter framing:
    asking this model to "do the task" makes it do the work in its own remote
    sandbox and report success, which never touches the user's machine.
    """
    specs = tool_specs(tools)
    if not specs:
        system = "\n\n".join(p for p in sys_parts if p)
        user = "\n\n".join(convo) if convo else "(no user message)"
        if len(convo) > 1:
            user = "Conversation so far. Respond to the FINAL message.\n\n" + user
        return system, user

    spec = "\n".join("- %s\n    params: %s\n    %s" % (n, json.dumps(p, ensure_ascii=False), d)
                      for n, p, d in specs)
    system = TOOL_PROTOCOL % spec
    if KEEP_INSTRUCTIONS:
        caller = "\n\n".join(p for p in sys_parts if p)
        if caller:
            system += ("\n\nCALLER INSTRUCTIONS (lower priority than the tool protocol):\n"
                       + caller)
    ctx = env_context(env_parts if env_parts is not None else sys_parts)
    if ctx:
        system += ("\n\nThe executor runs here. Use these real paths - never invent a "
                   "sandbox path like /codex_workspace/...:\n" + ctx)

    # Tool results arrive AFTER the last user message, so the transcript is
    # everything except that one line - not just what came before it.
    last_i = max((k for k, c in enumerate(convo) if c.startswith("[user]\n")), default=None)
    task = convo[last_i][len("[user]\n"):].strip() if last_i is not None else ""
    rest = (convo[:last_i] + convo[last_i + 1:]) if last_i is not None else list(convo)
    rest = clamp_transcript(rest)
    transcript = "\n\n".join(rest) if rest else "(empty - the executor has run nothing yet)"
    user = ("TASK:\n%s\n\nTRANSCRIPT SO FAR:\n%s" % (task or "(none)", transcript)
            + TOOL_REMINDER % ", ".join(n for n, _, _ in specs))
    return system, user


def _content_text(content, where="content"):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ClientInputError("%s must be a string or content-part array" % where)
    out = []
    for part in content:
        if not isinstance(part, dict):
            raise ClientInputError("%s contains a non-object content part" % where)
        typ = part.get("type")
        if typ in (None, "text", "input_text", "output_text"):
            if "text" not in part or not isinstance(part["text"], str):
                raise ClientInputError(
                    "%s text part must contain a string text field" % where)
            out.append(part["text"])
            continue
        raise ClientInputError(
            "unsupported %s part type %r; Prism adapter is text-only" % (where, typ))
    return "".join(out)


def flatten(messages, tools):
    """Chat-Completions shape."""
    if messages is None:
        messages = []
    if not isinstance(messages, list):
        raise ClientInputError("messages must be an array")
    sys_parts, convo = [], []
    for m in messages:
        if not isinstance(m, dict):
            raise ClientInputError("messages must contain objects")
        role = m.get("role")
        content = _content_text(m.get("content"), "chat message content")
        if role in ("system", "developer"):
            sys_parts.append(content)
        elif role == "tool":
            convo.append("[tool result]\n" + content[:8000])
        elif role == "assistant":
            if m.get("tool_calls"):
                convo.append("[executor ran]\n" + json.dumps(m["tool_calls"], ensure_ascii=False))
            if content:
                convo.append("[assistant]\n" + content)
        else:
            convo.append("[user]\n" + content)
    return assemble(sys_parts, convo, tools)


def flatten_responses(items, instructions, tools):
    """Responses shape - what Codex sends."""
    if isinstance(items, str):
        items = [items]
    elif items is None:
        items = []
    elif not isinstance(items, list):
        raise ClientInputError("Responses input must be a string or an array")

    sys_parts, convo, env_parts = [], [], []
    if instructions is not None:
        if not isinstance(instructions, str):
            raise ClientInputError("instructions must be a string")
        if instructions:
            sys_parts.append(instructions)
            env_parts.append(instructions)

    for it in items:
        if isinstance(it, str):
            convo.append("[user]\n" + it)
            continue
        if not isinstance(it, dict):
            raise ClientInputError("Responses input items must be strings or objects")
        t = it.get("type", "message")
        if t == "function_call":
            convo.append("[executor ran]\n%s %s" % (it.get("name"), it.get("arguments")))
        elif t == "function_call_output":
            out = it.get("output")
            if isinstance(out, (dict, list)):
                out = json.dumps(out, ensure_ascii=False)
            convo.append("[result]\n" + (out or "")[:8000])
        elif t == "custom_tool_call":
            convo.append("[executor ran]\n%s" % (it.get("input") or "")[:4000])
        elif t == "custom_tool_call_output":
            out = it.get("output")
            if isinstance(out, (dict, list)):
                out = json.dumps(out, ensure_ascii=False)
            convo.append("[result]\n" + (out or "")[:8000])
        elif t == "additional_tools":
            continue
        elif t == "message":
            role = it.get("role", "user")
            c = _content_text(it.get("content"), "Responses message content")
            env_parts.append(c)
            if role in ("developer", "system"):
                sys_parts.append(c)
            elif role == "user" and NOISE.match(c.lstrip()):
                continue
            else:
                convo.append(("[assistant]\n" if role == "assistant" else "[user]\n") + c)
        elif API_ONLY:
            raise ClientInputError(
                "unsupported Responses input item type %r; "
                "adapter supports text messages and function-call items" % t)

    extra = additional_tool_specs(items)
    if extra and not tool_specs(tools):
        tools = [{"type": "function", "name": n, "parameters": p, "description": d}
                 for _, n, p, d in extra]
    return assemble(sys_parts, convo, tools, env_parts)


def _strip_fence(text):
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    i = s.find("{")
    return s[i:] if i > 0 and i < 40 else s


def _loads(s):
    """Tolerant JSON: models drop trailing braces on long nested arguments."""
    if not s.startswith("{"):
        return None
    for extra in range(4):
        try:
            return json.loads(s + "}" * extra)
        except Exception:
            continue
    try:                                   # trailing prose after a valid object
        return json.JSONDecoder().raw_decode(s)[0]
    except Exception:
        return None


def parse_done(text):
    d = _loads(_strip_fence(text))
    if isinstance(d, dict) and isinstance(d.get("done"), str):
        return d["done"]
    return None


def parse_tool_call(text, allowed_names=None):
    """Pull {"tool_call":{...}} back out of the model's reply."""
    d = _loads(_strip_fence(text))
    if not isinstance(d, dict):
        return None
    tc = d.get("tool_call")
    if not isinstance(tc, dict) or not tc.get("name"):
        return None
    name = tc["name"]
    if allowed_names is not None and name not in allowed_names:
        raise ToolProtocolError("model emitted unknown tool %r" % name)
    args = tc.get("arguments", {})
    if isinstance(args, str):
        parsed = _loads(args)
        if parsed is None:
            raise ToolProtocolError("model emitted invalid JSON arguments for tool %s" % name)
        args = parsed
    if not isinstance(args, dict):
        raise ToolProtocolError("tool arguments for %s must be an object" % name)
    return {"id": "call_%s" % os.urandom(8).hex(), "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)}}


def keepalive_loop():
    """The sandbox goes cold when idle and then every call fails for a minute or
    two while we notice and re-capture. Cheaper to poke it on a timer."""
    period = float(os.environ.get("PRISM_KEEPALIVE", "600"))
    if period <= 0:
        return
    while True:
        time.sleep(period)
        try:
            call_prism(MODELS[0], "", "ping", "low", retries=1)
            sys.stderr.write("[free-astra %s] keepalive ok\n" % time.strftime("%H:%M:%S"))
        except Exception as e:
            sys.stderr.write("[free-astra %s] keepalive failed: %s\n"
                             % (time.strftime("%H:%M:%S"), str(e)[:160]))


def _prism_attempt(inp, model, effort, deadline, retries):
    """One pass at getting an answer. Returns (text, None) or (None, reason)."""
    last = ""
    for attempt in range(retries):
        if deadline - time.time() <= 5:
            return None, last or "out of time"
        meta = {"model": model, "reasoning_effort": effort, "frontend_origin": BASE,
                "sandbox_url": _session["sandbox_url"], "sandbox_token": _session["sandbox_token"]}
        if _session.get("project_id"):
            meta["projectId"] = _session["project_id"]
        if _session.get("user_id"):
            meta["userId"] = _session["user_id"]

        st, s = post("/api/llm/response_with_tools_start", {"input": inp, "metadata": meta},
                     timeout=max(60, min(deadline - time.time(), 180)))
        if st != 200 or "request_id" not in s:
            last = "start http=%s %s" % (st, json.dumps(s)[:300])
            sys.stderr.write("[free-astra %s] start failed http=%s body=%s\n"
                             % (time.strftime("%H:%M:%S"), st, json.dumps(s)[:400]))
            if st in (400, 401, 403):
                return None, last                  # auth-shaped: retrying cannot help
            time.sleep(min(3, max(0, deadline - time.time())))
            continue

        # `start` can finish the turn outright, and then it carries no turn_state.
        # Polling with a null one is rejected with "turn_state is required", which
        # used to look like an expired session and trigger a pointless refresh.
        if s.get("status") in ("completed", "error", "failed") or not s.get("turn_state"):
            rs = s.get("response") or {}
            if rs.get("status") == "success":
                out = rs.get("payload", {}).get("output") or []
                return "".join(c.get("text", "") for o in out
                               if o.get("type") == "message"
                               for c in o.get("content", [])), None
            pay = rs.get("payload") or {}
            body = (((pay.get("codexRequestDebug") or {}).get("error") or {})
                    .get("bodyText") or "")
            last = ("%s: %s %s" % (pay.get("reason"), pay.get("message", ""), body))[:400] \
                   or "start returned %s with no turn_state" % s.get("status")
            if unsupported(body):
                return None, last
            if pay.get("reason") in ("sandbox_reconnecting",) or pay.get("httpStatus") == 504:
                mint_sandbox()
            time.sleep(min(2, max(0, deadline - time.time())))
            continue

        p = {"request_id": s["request_id"], "turn_state": s["turn_state"]}
        while time.time() < deadline:
            st, j = post("/api/llm/response_with_tools_status", p,
                         timeout=min(max(deadline - time.time(), 5), 60))
            if st != 200:
                sys.stderr.write("[free-astra %s] poll failed http=%s body=%s\n"
                                 % (time.strftime("%H:%M:%S"), st, json.dumps(j)[:400]))
                return None, "status http=%s %s" % (st, json.dumps(j)[:300])
            if j.get("turn_state"):
                p["turn_state"] = j["turn_state"]
            if j.get("status") in ("completed", "error", "failed"):
                rs = j.get("response") or {}
                if rs.get("status") == "success":
                    out = rs.get("payload", {}).get("output") or []
                    return "".join(c.get("text", "") for o in out
                                   if o.get("type") == "message"
                                   for c in o.get("content", [])), None
                pay = rs.get("payload") or {}
                body = (((pay.get("codexRequestDebug") or {}).get("error") or {})
                        .get("bodyText") or "")
                last = ("%s: %s %s" % (pay.get("reason"), pay.get("message", ""), body))[:400]
                if unsupported(body):
                    return None, last           # a new sandbox will not help
                if pay.get("reason") in ("sandbox_reconnecting",) or pay.get("httpStatus") == 504:
                    mint_sandbox()                  # cold or dead sandbox
                break
            time.sleep(1.5)
        else:
            return None, "timed out after %.0fs" % BUDGET
        time.sleep(min(2, max(0, deadline - time.time())))
    return None, last or "no response"


def call_prism(model, system, user, effort, retries=3):
    waited = time.time()
    if not _turn_lock.acquire(timeout=QUEUE_TIMEOUT):
        raise BusyError("Prism sandbox is busy; retry later")
    try:
        return _call_prism_locked(model, system, user, effort, retries, waited)
    finally:
        _turn_lock.release()


def _call_prism_locked(model, system, user, effort, retries, queued_at):
    inp = []
    if system:
        inp.append({"type": "message", "role": "system",
                    "content": [{"type": "input_text", "text": system}]})
    inp.append({"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": user}]})

    # the queue wait already spent part of the caller's patience
    budget = max(30.0, BUDGET - (time.time() - queued_at))
    wanted = model
    model = _substitute.get(model, model)
    text, why = _prism_attempt(inp, model, effort, time.time() + budget, retries)
    if text is not None:
        return text

    for alt in FALLBACKS.get(wanted, []) if (ALLOW_FALLBACK and unsupported(why)) else []:
        if alt == model:
            continue
        sys.stderr.write("[free-astra %s] %s rejected by Prism, falling back to %s\n"
                         % (time.strftime("%H:%M:%S"), model, alt))
        text, why = _prism_attempt(inp, alt, effort, time.time() + budget, retries)
        if text is not None:
            _substitute[wanted] = alt          # stick with it for this process
            return text
        if not unsupported(why):
            break
    # Any terminal failure here usually means stale cookies or a dead sandbox, and
    # both the start and the poll side report it. Re-capture once, then try again.
    if "turn_state is required" in (why or "") or unsupported(why):
        raise RuntimeError(why)                 # not a session problem
    if try_refresh(why):
        text, why = _prism_attempt(inp, model, effort, time.time() + budget, retries)
        if text is not None:
            return text
    raise RuntimeError(
        "%s. Run %s/refresh-session.sh and retry." % (why, HERE))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        sys.stderr.write("[free-astra %s] %s\n"
                         % (time.strftime("%H:%M:%S"), fmt % a))

    def _clean_path(self):
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def _authorized(self):
        clean = self._clean_path()
        if clean in ("/healthz", "/readyz", "/v1/healthz", "/v1/readyz"):
            return True
        if not API_KEY:
            return True
        supplied = self.headers.get("Authorization", "").encode()
        if hmac.compare_digest(supplied, ("Bearer " + API_KEY).encode()):
            return True
        self._send(401, {"error": {"message": "invalid adapter API key",
                                    "type": "authentication_error"}}, close=True)
        return False

    def _sse_start(self):
        """HTTP/1.1 needs explicit framing. Without Content-Length or
        Transfer-Encoding the client cannot tell where the body ends and reports
        "stream disconnected before completion: error decoding response body"."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _sse(self, payload):
        data = payload if isinstance(payload, bytes) else payload.encode()
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def _sse_end(self, include_done=True):
        if include_done:
            self._sse(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _stream_error(self, message, kind, responses_api):
        if responses_api:
            payload = {"type": "error", "error": {"type": kind, "message": str(message)}}
            self._sse("event: error\ndata: %s\n\n" % json.dumps(payload))
            self._sse_end(include_done=False)
        else:
            self._sse("data: %s\n\n" % json.dumps(
                {"error": {"type": kind, "message": str(message)}}))
            self._sse_end(include_done=True)

    def _call_with_heartbeat(self, fn):
        if SSE_HEARTBEAT <= 0:
            return fn()
        done = threading.Event()
        box = {}

        def worker():
            try:
                box["value"] = fn()
            except BaseException as e:
                box["error"] = e
            finally:
                done.set()

        threading.Thread(target=worker, daemon=True).start()
        while not done.wait(SSE_HEARTBEAT):
            self._sse(b": ping\n\n")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _send(self, code, obj, ctype="application/json", close=False):
        body = (obj if isinstance(obj, bytes) else json.dumps(obj).encode())
        if close:
            self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        clean = self._clean_path()
        allowed = {
            "/v1/models", "/v1/responses", "/v1/chat/completions",
            "/healthz", "/readyz", "/v1/healthz", "/v1/readyz",
        }
        if API_ONLY and clean not in allowed:
            return self._send(404, {"error": {"message": "route is not served by the Prism adapter",
                                               "type": "unsupported_route"}})
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()


    def do_GET(self):
        if not self._authorized():
            return
        clean = self._clean_path()
        if clean in ("/healthz", "/v1/healthz"):
            return self._send(200, {"status": "ok"})
        if clean in ("/readyz", "/v1/readyz"):
            ready = bool(_session.get("cookie") and _session.get("sandbox_token"))
            return self._send(200 if ready else 503,
                              {"status": "ready" if ready else "not_ready"})
        if clean == "/v1/models":
            mf = MANIFEST
            if "client_version=" in self.path and os.path.exists(mf) and not API_ONLY:
                with open(mf, encoding="utf-8") as fh:
                    mine = [m for m in json.load(fh)["models"] if m["slug"] in ALIAS]
                out = []
                try:
                    out = list(upstream_models().get("models") or [])
                except Exception as e:
                    sys.stderr.write("[free-astra %s] upstream models failed: %s\n"
                                     % (time.strftime("%H:%M:%S"), e))
                for m in out + mine:
                    m["prefer_websockets"] = False
                return self._send(200, {"models": out + mine})
            ids = list(ALIAS.keys())
            return self._send(200, {"object": "list", "data": [
                {"id": m, "object": "model", "created": 0, "owned_by": "prism"}
                for m in ids]})
        if clean == "/v1/responses":
            return self._send(426, {"error": "websocket transport not supported"})
        self._passthrough(method="GET")


    # headers that describe OUR hop, not the payload, so they must not be copied
    HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer",
           "upgrade", "proxy-authorization", "proxy-authenticate", "content-length"}

    def _passthrough(self, raw_body=None, method="POST"):
        """Anything we do not own - forward it to the real backend verbatim.

        We are the single front door for Codex, so this has to cover every route
        it uses, not just chat: image generation calls /v1/images/generations and
        /v1/images/edits, and a 404 there breaks imagegen with a local reference
        image.
        """
        if API_ONLY:
            return self._send(404, {"error": {"message": "route is not served by the Prism adapter",
                                               "type": "unsupported_route"}})
        skip = {"host", "content-length", "connection", "accept-encoding"}
        headers = {k: v for k, v in self.headers.items() if k.lower() not in skip}
        url = UPSTREAM + self.path.split("/v1", 1)[-1]
        req = urllib.request.Request(url, data=raw_body, method=method, headers=headers)
        try:
            up = urllib.request.urlopen(req, timeout=900)
        except urllib.error.HTTPError as e:
            up = e                       # an error response is still a response
        except Exception as e:
            sys.stderr.write("[free-astra %s] upstream %s %s failed: %s\n"
                             % (time.strftime("%H:%M:%S"), method, self.path, str(e)[:200]))
            return self._send(502, {"error": {"message": "upstream: %s" % e}})

        try:
            ctype = up.headers.get("Content-Type", "application/json")
            streaming = ("event-stream" in ctype
                         or up.headers.get("Transfer-Encoding", "").lower() == "chunked")
            if not streaming:
                body = up.read()
                self.send_response(up.status)
                for k, v in up.headers.items():
                    if k.lower() not in self.HOP:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(up.status)
            for k, v in up.headers.items():
                if k.lower() not in self.HOP:
                    self.send_header(k, v)   # keep content-encoding et al intact
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                # read1, not read: read(n) blocks until n bytes arrive, which stalls
                # an SSE stream until Codex gives up with "stream disconnected".
                chunk = up.read1(65536) if hasattr(up, "read1") else up.read(1)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                up.close()
            except Exception:
                pass

    def _responses_out(self, req, model, text, tc, ns_of=None, stream_started=False):
        rid = "resp_%s" % os.urandom(12).hex()
        created = int(time.time())
        if tc:
            item = {"type": "function_call", "id": "fc_%s" % os.urandom(12).hex(),
                    "call_id": tc["id"], "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"], "status": "completed"}
            ns = (ns_of or {}).get(tc["function"]["name"])
            if ns and ns != "functions":
                item["namespace"] = ns
        else:
            item = {"type": "message", "id": "msg_%s" % os.urandom(12).hex(),
                    "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                 "input_tokens_details": {"cached_tokens": 0},
                 "output_tokens_details": {"reasoning_tokens": 0}}
        base = {"id": rid, "object": "response", "created_at": created, "model": model,
                "status": "completed", "output": [item], "usage": usage,
                "metadata": req.get("metadata") or {}}

        if not req.get("stream"):
            return self._send(200, base)
        if not stream_started:
            self._sse_start()

        seq = [0]
        def ev(kind, payload):
            payload = dict(payload, type=kind, sequence_number=seq[0])
            seq[0] += 1
            self._sse("event: %s\ndata: %s\n\n" % (kind, json.dumps(payload)))

        shell = dict(base, status="in_progress", output=[], usage=None)
        ev("response.created", {"response": shell})
        ev("response.in_progress", {"response": shell})

        if tc:
            pending = dict(item, status="in_progress", arguments="")
            ev("response.output_item.added",
               {"response_id": rid, "output_index": 0, "item": pending})
            args = item["arguments"]
            for i in range(0, len(args), 600):
                ev("response.function_call_arguments.delta",
                   {"response_id": rid, "item_id": item["id"], "output_index": 0,
                    "delta": args[i:i + 600]})
            ev("response.function_call_arguments.done",
               {"response_id": rid, "item_id": item["id"], "output_index": 0,
                "name": item["name"], "arguments": args})
        else:
            pending = dict(item, status="in_progress",
                           content=[{"type": "output_text", "text": "", "annotations": []}])
            ev("response.output_item.added",
               {"response_id": rid, "output_index": 0, "item": pending})
            ev("response.content_part.added",
               {"response_id": rid, "item_id": item["id"], "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []}})
            for i in range(0, len(text), 600):
                ev("response.output_text.delta",
                   {"response_id": rid, "item_id": item["id"], "output_index": 0,
                    "content_index": 0, "delta": text[i:i + 600]})
            ev("response.output_text.done",
               {"response_id": rid, "item_id": item["id"], "output_index": 0,
                "content_index": 0, "text": text})
            ev("response.content_part.done",
               {"response_id": rid, "item_id": item["id"], "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": []}})

        ev("response.output_item.done",
           {"response_id": rid, "output_index": 0, "item": item})
        ev("response.completed", {"response": base})
        self._sse_end(include_done=False)

    def do_PUT(self):
        if not self._authorized():
            return
        if API_ONLY:
            return self._send(405, {"error": {"message": "method not allowed",
                                               "type": "unsupported_route"}}, close=True)
        self._passthrough(self.rfile.read(int(self.headers.get("Content-Length") or 0)), "PUT")

    def do_PATCH(self):
        if not self._authorized():
            return
        if API_ONLY:
            return self._send(405, {"error": {"message": "method not allowed",
                                               "type": "unsupported_route"}}, close=True)
        self._passthrough(self.rfile.read(int(self.headers.get("Content-Length") or 0)), "PATCH")

    def do_DELETE(self):
        if not self._authorized():
            return
        if API_ONLY:
            return self._send(405, {"error": {"message": "method not allowed",
                                               "type": "unsupported_route"}}, close=True)
        self._passthrough(method="DELETE")

    def do_POST(self):
        if not self._authorized():
            return
        clean = self._clean_path()
        is_resp = clean == "/v1/responses"
        is_chat = clean == "/v1/chat/completions"
        if not is_resp and not is_chat:
            if API_ONLY:
                return self._send(404, {"error": {"message": "route is not served by the Prism adapter",
                                                   "type": "unsupported_route"}}, close=True)
            return self._passthrough(
                self.rfile.read(int(self.headers.get("Content-Length") or 0)))

        transfer_encodings = self.headers.get_all("Transfer-Encoding") or []
        content_lengths = self.headers.get_all("Content-Length") or []
        transfer_encoding = ",".join(transfer_encodings).strip().lower()
        content_length = content_lengths[0] if len(content_lengths) == 1 else None
        if API_ONLY and transfer_encoding:
            return self._send(400, {"error": {
                "message": "Transfer-Encoding is not supported in API-only mode",
                "type": "invalid_request_error", "param": "Transfer-Encoding"}}, close=True)
        if API_ONLY and len(content_lengths) != 1:
            return self._send(411 if not content_lengths else 400, {"error": {
                "message": ("Content-Length is required in API-only mode"
                            if not content_lengths
                            else "exactly one Content-Length header is required"),
                "type": "invalid_request_error", "param": "Content-Length"}}, close=True)
        if content_length is not None and not (
                content_length.isascii() and content_length.isdigit()):
            return self._send(400, {"error": {
                "message": "Content-Length must be a non-negative integer",
                "type": "invalid_request_error", "param": "Content-Length"}}, close=True)
        n = int(content_length) if content_length is not None else 0
        if n > MAX_BODY:
            return self._send(413, {"error": {"message": "request body too large",
                                               "type": "invalid_request_error"}}, close=True)
        raw_body = self.rfile.read(n)
        enc = (self.headers.get("Content-Encoding") or "").strip().lower()
        if API_ONLY and enc and enc != "identity":
            return self._send(415, {"error": {
                "message": "compressed request bodies are disabled in API-only mode",
                "type": "unsupported_media_type",
                "param": "Content-Encoding"}})
        try:
            body = decompress(raw_body, enc) if enc and enc != "identity" else raw_body
        except Exception as e:
            return self._send(400, {"error": {"message": "request decompression failed: %s" % e,
                                               "type": "invalid_request_error"}})
        if len(body) > MAX_BODY:
            return self._send(413, {"error": {"message": "decompressed request body too large",
                                               "type": "invalid_request_error"}})
        try:
            req = json.loads(body or b"{}")
        except Exception as e:
            return self._send(400, {"error": {"message": "bad json: %s" % e,
                                               "type": "invalid_request_error"}})
        if not isinstance(req, dict):
            return self._send(400, {"error": {"message": "request JSON must be an object",
                                               "type": "invalid_request_error"}})

        if os.environ.get("PRISM_DUMP"):
            with open(os.path.join(HERE, "last_request.json"), "w", encoding="utf-8") as fh:
                json.dump(req, fh, ensure_ascii=False, indent=2)

        if API_ONLY and ("model" not in req or req.get("model") is None):
            return self._send(400, {"error": {
                "message": "model is required in API-only mode",
                "type": "invalid_request_error", "param": "model"}})
        requested = req.get("model", "prism-astra")
        if requested is None:
            requested = "prism-astra"
        if not isinstance(requested, str) or not requested:
            return self._send(400, {"error": {
                "message": "model must be a non-empty string",
                "type": "invalid_request_error", "param": "model"}})
        effort = DEFAULT_EFFORT
        if ":" in requested:
            requested, effort = requested.rsplit(":", 1)
        if requested not in ALIAS:
            if API_ONLY:
                return self._send(400, {"error": {"message": "unknown Prism model %r" % requested,
                                                   "type": "model_not_found",
                                                   "param": "model"}})
            return self._passthrough(raw_body)
        model = ALIAS[requested]

        if API_ONLY and is_resp and ("input" not in req or req.get("input") is None):
            return self._send(400, {"error": {
                "message": "input is required in API-only mode",
                "type": "invalid_request_error", "param": "input"}})
        if API_ONLY and is_chat and ("messages" not in req or req.get("messages") is None):
            return self._send(400, {"error": {
                "message": "messages is required in API-only mode",
                "type": "invalid_request_error", "param": "messages"}})

        if is_resp:
            for param in ("previous_response_id", "conversation"):
                if req.get(param):
                    return self._send(400, {"error": {
                        "message": "%s is not supported by the Prism adapter" % param,
                        "type": "unsupported_parameter", "param": param}})
            if "background" in req and not isinstance(req["background"], bool):
                return self._send(400, {"error": {
                    "message": "background must be a boolean",
                    "type": "invalid_request_error", "param": "background"}})
            if req.get("background") is True:
                return self._send(400, {"error": {
                    "message": "background responses are not supported by the Prism adapter",
                    "type": "unsupported_parameter", "param": "background"}})
            if "store" in req and not isinstance(req["store"], bool):
                return self._send(400, {"error": {
                    "message": "store must be a boolean",
                    "type": "invalid_request_error", "param": "store"}})
            if req.get("store") is True:
                return self._send(400, {"error": {
                    "message": "stored responses are not supported by the Prism adapter",
                    "type": "unsupported_parameter", "param": "store"}})
            reasoning = req.get("reasoning")
            if reasoning is not None and not isinstance(reasoning, dict):
                return self._send(400, {"error": {
                    "message": "reasoning must be an object",
                    "type": "invalid_request_error", "param": "reasoning"}})
            reasoning = reasoning or {}
            if "effort" in reasoning:
                value = reasoning["effort"]
                if not isinstance(value, str) or not value:
                    return self._send(400, {"error": {
                        "message": "reasoning.effort must be a non-empty string",
                        "type": "invalid_request_error", "param": "reasoning.effort"}})
                effort = value
            elif "reasoning_effort" in req:
                value = req["reasoning_effort"]
                if not isinstance(value, str) or not value:
                    return self._send(400, {"error": {
                        "message": "reasoning_effort must be a non-empty string",
                        "type": "invalid_request_error", "param": "reasoning_effort"}})
                effort = value
            if "text" in req:
                text_cfg = req["text"]
                if not isinstance(text_cfg, dict):
                    return self._send(400, {"error": {
                        "message": "text must be an object",
                        "type": "invalid_request_error", "param": "text"}})
                if "format" in text_cfg:
                    fmt = text_cfg["format"]
                    if not isinstance(fmt, dict):
                        return self._send(400, {"error": {
                            "message": "text.format must be an object",
                            "type": "invalid_request_error", "param": "text.format"}})
                    if fmt.get("type") not in (None, "text"):
                        return self._send(400, {"error": {
                            "message": "structured output is not supported by the Prism adapter",
                            "type": "unsupported_parameter", "param": "text.format"}})
        else:
            if "reasoning_effort" in req:
                value = req["reasoning_effort"]
                if not isinstance(value, str) or not value:
                    return self._send(400, {"error": {
                        "message": "reasoning_effort must be a non-empty string",
                        "type": "invalid_request_error", "param": "reasoning_effort"}})
                effort = value
            if "response_format" in req:
                response_format = req["response_format"]
                if not isinstance(response_format, dict):
                    return self._send(400, {"error": {
                        "message": "response_format must be an object",
                        "type": "invalid_request_error", "param": "response_format"}})
                if response_format.get("type") not in (None, "text"):
                    return self._send(400, {"error": {
                        "message": "structured output is not supported by the Prism adapter",
                        "type": "unsupported_parameter", "param": "response_format"}})

        if "stream" in req and not isinstance(req.get("stream"), bool):
            return self._send(400, {"error": {
                "message": "stream must be a boolean",
                "type": "invalid_request_error", "param": "stream"}})

        if "tools" in req:
            tools = req["tools"]
            if not isinstance(tools, list):
                return self._send(400, {"error": {"message": "tools must be an array",
                                                   "type": "invalid_request_error",
                                                   "param": "tools"}})
        else:
            tools = []
        if ("parallel_tool_calls" in req
                and not isinstance(req["parallel_tool_calls"], bool)):
            return self._send(400, {"error": {
                "message": "parallel_tool_calls must be a boolean",
                "type": "invalid_request_error", "param": "parallel_tool_calls"}})
        if req.get("parallel_tool_calls") is True and tools:
            return self._send(400, {"error": {
                "message": "parallel tool calls are not supported by the Prism adapter",
                "type": "unsupported_parameter", "param": "parallel_tool_calls"}})

        try:
            if is_resp:
                system, user = flatten_responses(req.get("input"), req.get("instructions"), tools)
            else:
                messages = req["messages"] if "messages" in req else []
                system, user = flatten(messages, tools)
        except ClientInputError as e:
            return self._send(400, {"error": {"message": str(e),
                                               "type": "invalid_request_error"}})

        stream = bool(req.get("stream"))
        if stream:
            self._sse_start()
        try:
            invoke = lambda: call_prism(model, system, user, effort)
            text = self._call_with_heartbeat(invoke) if stream else invoke()
        except BusyError as e:
            if stream:
                return self._stream_error(str(e), "prism_busy", is_resp)
            return self._send(503, {"error": {"message": str(e), "type": "prism_busy"}})
        except Exception as e:
            sys.stderr.write("[free-astra %s] 502 model=%s sys=%dB user=%dB: %s\n"
                             % (time.strftime("%H:%M:%S"), model,
                                len(system), len(user), str(e)[:300]))
            if stream:
                return self._stream_error(str(e), "prism_upstream", is_resp)
            return self._send(502, {"error": {"message": str(e), "type": "prism_upstream"}})

        extra = additional_tool_specs(req.get("input")) if is_resp else []
        if extra and not tools:
            tools = [{"type": "function", "name": n, "parameters": p, "description": d}
                     for _, n, p, d in extra]
        ns_of = {n: ns for ns, n, _, _ in extra}
        try:
            allowed_tools = {n for n, _, _ in tool_specs(tools)}
            tc = parse_tool_call(text, allowed_tools) if allowed_tools else None
        except (ClientInputError, ToolProtocolError) as e:
            if stream:
                return self._stream_error(str(e), "tool_protocol_error", is_resp)
            return self._send(502, {"error": {"message": str(e),
                                               "type": "tool_protocol_error"}})
        if tc is None:
            done = parse_done(text)
            if done is not None:
                text = done

        sys.stderr.write("[free-astra %s] %s tools=%d sys=%dB user=%dB -> %s | %s\n" % (
            time.strftime("%H:%M:%S"),
            "ns" if extra else "top", len(tools), len(system), len(user),
            "TOOLCALL:" + tc["function"]["name"] if tc else "TEXT",
            repr(text[:160])))

        if is_resp:
            return self._responses_out(req, requested, text, tc, ns_of,
                                       stream_started=stream)

        msg = {"role": "assistant", "content": None if tc else text}
        if tc:
            msg["tool_calls"] = [tc]
        finish = "tool_calls" if tc else "stop"
        cid = "chatcmpl-%s" % os.urandom(8).hex()
        created = int(time.time())

        if not stream:
            return self._send(200, {
                "id": cid, "object": "chat.completion", "created": created,
                "model": requested,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})

        def chunk(delta, fin=None):
            d = {"id": cid, "object": "chat.completion.chunk", "created": created,
                 "model": requested,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
            self._sse("data: " + json.dumps(d) + "\n\n")

        chunk({"role": "assistant"})
        if tc:
            chunk({"tool_calls": [dict(index=0, **tc)]})
        elif text:
            for i in range(0, len(text), 600):
                chunk({"content": text[i:i + 600]})
        chunk({}, finish)
        self._sse_end(include_done=True)


def demo():
    """Self-check: prompt assembly + parsing, no network."""
    s, u = flatten([{"role": "system", "content": "be terse"},
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                    {"role": "user", "content": "bye"}], None)
    assert s == "be terse" and u.count("[user]") == 2 and u.rstrip().endswith("bye")

    tools = [{"type": "function", "name": "exec_command", "parameters": {"cmd": "string"}},
             {"type": "namespace", "name": "mcp__x", "tools": []},
             {"type": "web_search"}]
    assert len(tool_specs(tools)) == 1

    s3, u3 = flatten_responses(
        [{"type": "message", "role": "developer",
          "content": [{"type": "input_text", "text": "<environment_context>cwd=/tmp</environment_context>"}]},
         {"type": "message", "role": "user",
          "content": [{"type": "input_text",
                       "text": "<recommended_plugins>noise</recommended_plugins>\n"
                               "<cwd>/srv/app</cwd>"}]},
         {"type": "function_call", "name": "exec_command", "arguments": "{}"},
         {"type": "function_call_output", "output": "a.txt"},
         {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "make it work"}]}],
        None, tools)
    assert s3.startswith("<role>next-action emitter</role>")
    assert "- exec_command\n" in s3 and "mcp__x" not in s3
    assert "cwd=/tmp" in s3                      # environment kept
    # the cwd rides inside the same message we drop as noise - keep it anyway
    assert "<cwd>/srv/app</cwd>" in s3
    assert "recommended_plugins" not in u3       # scaffolding dropped
    assert u3.startswith("TASK:\nmake it work")  # real task spotlighted
    assert "[executor ran]" in u3 and "a.txt" in u3   # results after the task still kept
    assert u3.rstrip().endswith("=" * 80)

    tc = parse_tool_call('```json\n{"tool_call":{"name":"shell","arguments":{"command":["ls"]}}}\n```')
    assert tc["function"]["name"] == "shell"
    assert json.loads(tc["function"]["arguments"])["command"] == ["ls"]
    assert parse_tool_call("just prose") is None and parse_tool_call('{"not_a_tool":1}') is None
    broken = '{"tool_call":{"name":"exec_command","arguments":{"cmd":"ls","max_output_tokens":1000}}'
    assert parse_tool_call(broken)["function"]["name"] == "exec_command"   # missing brace repaired
    assert json.loads(parse_tool_call(broken)["function"]["arguments"])["cmd"] == "ls"
    assert parse_tool_call('{"tool_call":{"name":"x","arguments":"{\\"a\\":1}"}}')
    assert parse_done('{"done":"ok"} trailing prose') == "ok"

    # a long unrelated message that merely mentions cwd must not leak in
    noise = "## Memory\n" + "notes about another project " * 400 + "\ncwd stuff"
    assert env_context([noise]) == "" or "another project" not in env_context([noise])
    assert env_context(["<environment_context>cwd=/srv</environment_context>"]) \
           == "<environment_context>cwd=/srv</environment_context>"
    assert env_context(["junk <cwd>/a/b</cwd> junk"]) == "<cwd>/a/b</cwd>"
    assert env_context(["<cwd>/a</cwd>", "<cwd>/a</cwd>"]) == "<cwd>/a</cwd>"

    big = ["step %d %s" % (i, "x" * 500) for i in range(100)]
    clamped = clamp_transcript(big, budget=2000)
    assert sum(len(c) for c in clamped) < 3000
    assert "earlier steps omitted" in clamped[0] and clamped[-1] == big[-1]
    assert clamp_transcript([], budget=100) == []
    only = clamp_transcript(["y" * 5000], budget=1000)
    assert only[0].startswith("...(truncated)...") and len(only[0]) < 1200

    # code mode: Codex ships tools inside an additional_tools item, entry point `exec`
    at = [{"type": "additional_tools", "role": "developer", "tools": [
              {"type": "namespace", "name": "functions", "tools": [
                  {"type": "custom", "name": "apply_patch", "description": "freeform"},
                  {"type": "function", "name": "wait", "parameters": {}}]},
              {"type": "namespace", "name": "mcp__big", "tools": [
                  {"type": "function", "name": "noisy", "parameters": {}}]}]},
          {"type": "message", "role": "user",
           "content": [{"type": "input_text", "text": "do it"}]},
          {"type": "custom_tool_call", "name": "exec", "input": "await tools.exec_command({cmd:'ls'})"},
          {"type": "custom_tool_call_output", "output": "a.txt"}]
    extra = additional_tool_specs(at)
    assert [(ns, n) for ns, n, _, _ in extra] == [("functions", "wait")]  # custom+MCP dropped
    s4, u4 = flatten_responses(at, None, None)
    assert s4.startswith("<role>next-action emitter</role>") and "- wait\n" in s4
    assert u4.startswith("TASK:\ndo it") and "[executor ran]" in u4 and "a.txt" in u4
    assert parse_done('{"done":"all good"}') == "all good"
    assert parse_done('{"tool_call":{"name":"x"}}') is None
    print("demo ok")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo(); raise SystemExit
    load_session()
    UPSTREAM = pick_upstream()
    print("upstream: %s" % UPSTREAM)
    print("free-astra on http://%s:%d/v1  models=%s  effort=%s  api_only=%s"
          % (BIND, PORT, ",".join(ALIAS.keys()), DEFAULT_EFFORT, API_ONLY))
    globals()["UPSTREAM"] = UPSTREAM
    threading.Thread(target=keepalive_loop, daemon=True).start()
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()

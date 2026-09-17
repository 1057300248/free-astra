#!/usr/bin/env python3
"""Turn a DevTools "Copy as cURL" of one Prism request into session.json.

In the Prism tab: DevTools > Network, send one chat message, right-click the
`response_with_tools_start` request > Copy > Copy as cURL, then run:

    python3 scripts/from_curl.py            # reads the clipboard, or stdin
    pbpaste | python3 scripts/from_curl.py  # explicit

The request body carries a WARM sandbox token, which is the whole point: a token
minted from scratch points at a cold sandbox that boots for minutes and then 504s.
"""
import json, os, re, subprocess, sys

STATE = os.environ.get("FREE_ASTRA_HOME", os.path.expanduser("~/.free-astra"))
OUT = os.path.join(STATE, "session.json")


def read_input():
    if not sys.stdin.isatty():
        data = sys.stdin.read()
        if data.strip():
            return data
    for cmd in (["pbpaste"], ["xclip", "-o", "-selection", "clipboard"], ["wl-paste"]):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.decode("utf-8", "replace")
        except Exception:
            continue
    sys.exit("nothing on stdin or the clipboard - paste the cURL and pipe it in")


def unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        s = s[1:-1]
    return s.replace("\\'", "'").replace('\\"', '"')


def main():
    raw = read_input().replace("\\\n", " ").replace("^\n", " ")
    if "response_with_tools_start" not in raw:
        sys.exit("that cURL is not the response_with_tools_start request - copy that "
                 "one specifically (it is the POST sent when you send a chat message)")

    cookie = ""
    for m in re.finditer(r"-H\s+('(?:[^']|\\')*'|\"(?:[^\"]|\\\")*\")", raw):
        h = unquote(m.group(1))
        if h.lower().startswith("cookie:"):
            cookie = h.split(":", 1)[1].strip()
    if not cookie:
        m = re.search(r"-b\s+('(?:[^']|\\')*'|\"(?:[^\"]|\\\")*\")", raw)
        if m:
            cookie = unquote(m.group(1))
    if not cookie:
        sys.exit("no Cookie header found in that cURL")

    body = None
    for flag in ("--data-raw", "--data-binary", "--data", "-d"):
        m = re.search(re.escape(flag) + r"\s+('(?:[^']|\\')*'|\"(?:[^\"]|\\\")*\")", raw)
        if m:
            body = unquote(m.group(1))
            break
    if body is None:
        sys.exit("no request body found in that cURL")

    try:
        meta = json.loads(body).get("metadata") or {}
    except Exception as e:
        sys.exit("could not parse the request body as JSON: %s" % e)
    if not meta.get("sandbox_token"):
        sys.exit("that request carries no sandbox_token - make sure you copied the "
                 "POST to /api/llm/response_with_tools_start")

    os.makedirs(STATE, exist_ok=True)
    old = {}
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            pass
    session = {
        "cookie": cookie,
        "sandbox_url": meta["sandbox_url"],
        "sandbox_token": meta["sandbox_token"],
        "project_id": meta.get("projectId"),
        "user_id": meta.get("userId"),
        "project_url": old.get("project_url", ""),
        "upstream": old.get("upstream"),
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(session, f, indent=2)
    os.chmod(OUT, 0o600)
    print("wrote %s (cookie %d bytes, sandbox token %d bytes)"
          % (OUT, len(cookie), len(meta["sandbox_token"])))


if __name__ == "__main__":
    main()

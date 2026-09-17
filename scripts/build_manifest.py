#!/usr/bin/env python3
"""Build the model manifest free-astra serves to Codex.

The three prism entries are cloned from your own account's Codex model catalog and
changed in only the ways that matter, so they stay consistent with whatever OpenAI
ships. Nothing here is committed to the repo: the upstream entries carry OpenAI's
own instruction text, which is theirs, not ours.
"""
import json, os, sys, urllib.request

STATE = os.environ.get("FREE_ASTRA_HOME", os.path.expanduser("~/.free-astra"))
OUT = os.path.join(STATE, "models.json")
UPSTREAM = "https://chatgpt.com/backend-api/codex"
CLONE = [("gpt-6-astra", "prism-astra", "Prism Astra"),
         ("gpt-5.6-sol", "prism-sol", "Prism Sol"),
         ("gpt-5.6-terra", "prism-terra", "Prism Terra")]


def official():
    auth = os.path.expanduser("~/.codex/auth.json")
    if not os.path.exists(auth):
        sys.exit("~/.codex/auth.json not found - sign in with `codex login` first")
    d = json.load(open(auth, encoding="utf-8"))
    tok = (d.get("tokens") or {}).get("access_token")
    acct = (d.get("tokens") or {}).get("account_id") or ""
    if not tok:
        sys.exit("no access token in ~/.codex/auth.json - run `codex login`")
    req = urllib.request.Request(
        UPSTREAM + "/models?client_version=0.154.0",
        headers={"authorization": "Bearer " + tok, "chatgpt-account-id": acct,
                 "accept": "application/json", "originator": "codex_cli_rs",
                 "user-agent": "codex-cli/0.154.0"})
    with urllib.request.urlopen(req, timeout=30) as f:
        return {m["slug"]: m for m in json.loads(f.read().decode())["models"]}


def main():
    have = official()
    missing = [s for s, _, _ in CLONE if s not in have]
    if missing:
        sys.exit("your account has no access to %s - free-astra can only expose "
                 "models you already have" % ", ".join(missing))
    out = []
    for src, slug, label in CLONE:
        m = dict(have[src])
        m["slug"] = slug
        m["display_name"] = label
        m["description"] = "Via prism.openai.com (separate quota)"
        m["tool_mode"] = "direct"        # plain function tools we can emulate
        m["prefer_websockets"] = False   # we serve plain HTTP
        m["priority"] = 0
        m["multi_agent_version"] = None
        m["experimental_supported_tools"] = []
        m["supports_parallel_tool_calls"] = False
        for k in ("available_in_plans", "minimal_client_version",
                  "availability_nux", "available_access_programs"):
            m.pop(k, None)               # plan/version gates would hide us in the picker
        out.append(m)
    os.makedirs(STATE, exist_ok=True)
    json.dump({"models": out}, open(OUT, "w"), indent=1)
    print("wrote %s (%s)" % (OUT, ", ".join(m["slug"] for m in out)))


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# free-astra: pull a fresh Prism session (cookies + a WARM sandbox token) out of
# the logged-in Chrome, using the chrome-use CLI. Optional - scripts/from_curl.py
# does the same thing from a DevTools "Copy as cURL" with no dependencies.
#
# The token must be warm: minting one directly gives a cold sandbox that boots for
# minutes and then 504s. Letting the Prism page send one real chat message makes the
# app establish and warm a sandbox with its own reconnect logic; we record that
# request with a HAR and lift the metadata it actually used.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${FREE_ASTRA_HOME:-$HOME/.free-astra}"
SESSION="${PRISM_SESSION:-$STATE/session.json}"
PROJECT_URL="${PRISM_PROJECT_URL:-}"
S=(chrome-use --session prism)

if [ -z "$PROJECT_URL" ] && [ -f "$SESSION" ]; then
  PROJECT_URL="$(python3 -c "
import json
try: print(json.load(open('$SESSION')).get('project_url',''))
except Exception: print('')")"
fi
[ -n "$PROJECT_URL" ] || { echo "set PRISM_PROJECT_URL to your Prism project URL" >&2; exit 1; }
command -v chrome-use >/dev/null || {
  echo "chrome-use not found. Install it:" >&2
  echo "  curl -fsSL https://raw.githubusercontent.com/leeguooooo/chrome-use/main/install.sh | sh" >&2
  echo "or capture the session by hand: python3 scripts/from_curl.py" >&2
  exit 1; }
mkdir -p "$STATE"

echo "[1/5] opening $PROJECT_URL"
"${S[@]}" open "$PROJECT_URL" --reuse-tab >/dev/null
sleep 3

echo "[2/5] recording"
"${S[@]}" network requests --clear >/dev/null 2>&1 || true
"${S[@]}" network har start "$DIR/.refresh.har" >/dev/null

echo "[3/5] sending one chat message"
# Two textareas exist: CodeMirror's IME helper (no placeholder) and the chat input.
tag="$("${S[@]}" eval '(()=>{const t=[...document.querySelectorAll("textarea")].find(x=>x.offsetParent!==null&&(x.placeholder||"").trim()&&!x.classList.contains("ime-text-area"));if(!t)return "none";t.setAttribute("data-prism-chat","1");return t.placeholder;})()' 2>/dev/null | tail -1)"
if [ "$tag" = '"none"' ]; then
  echo "  chat box not found - send one message yourself in the Prism tab, then re-run" >&2
else
  echo "  chat box: $tag"
  "${S[@]}" fill 'textarea[data-prism-chat]' "ping" >/dev/null
  "${S[@]}" press Enter --selector 'textarea[data-prism-chat]' >/dev/null
fi

echo "[4/5] waiting for the request to land"
for i in $(seq 1 20); do
  sleep 5
  if "${S[@]}" network requests 2>/dev/null | grep -q 'response_with_tools_start'; then
    echo "  captured on attempt $i"; sleep 3; break
  fi
  echo "  waiting ($i)"
done
"${S[@]}" network har stop "$DIR/.refresh.har" >/dev/null

echo "[5/5] writing $SESSION"
"${S[@]}" cookies get 2>/dev/null | grep -E '^[A-Za-z_][A-Za-z0-9_.-]*=' > "$DIR/.cookies.tmp"
PRISM_PROJECT_URL="$PROJECT_URL" SESSION="$SESSION" DIR="$DIR" python3 - <<'PY'
import json, os, sys
d = os.environ['DIR']
cookie = '; '.join(l.strip() for l in open(d + '/.cookies.tmp', encoding='utf-8') if l.strip())
har = json.load(open(d + '/.refresh.har', encoding='utf-8'))
meta = None
for e in har['log']['entries']:                      # last one wins = freshest
    if 'response_with_tools_start' in e['request']['url']:
        body = (e['request'].get('postData') or {}).get('text')
        if body:
            m = json.loads(body).get('metadata')
            if m and m.get('sandbox_token'):
                meta = m
if not meta:
    sys.exit('no warm sandbox token in the capture - open the Prism tab, send a chat '
             'message by hand, then re-run this script')
old = {}
if os.path.exists(os.environ['SESSION']):
    try: old = json.load(open(os.environ['SESSION'], encoding='utf-8'))
    except Exception: pass
json.dump({'cookie': cookie,
           'sandbox_url': meta['sandbox_url'],
           'sandbox_token': meta['sandbox_token'],
           'project_id': meta.get('projectId'),
           'user_id': meta.get('userId'),
           'project_url': os.environ['PRISM_PROJECT_URL'],
           'upstream': old.get('upstream')},
          open(os.environ['SESSION'], 'w'), indent=2)
os.chmod(os.environ['SESSION'], 0o600)
print('ok: cookie %d bytes, sandbox token %d bytes' % (len(cookie), len(meta['sandbox_token'])))
PY
rm -f "$DIR/.cookies.tmp" "$DIR/.refresh.har"

#!/usr/bin/env bash
# free-astra installer. Run it from the repo root:
#   ./install.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${FREE_ASTRA_HOME:-$HOME/.free-astra}"
PORT="${PRISM_PORT:-8319}"
mkdir -p "$STATE"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "free-astra installer"

# ---------------------------------------------------------------- prerequisites
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
command -v codex   >/dev/null || { echo "the codex CLI is required: npm i -g @openai/codex" >&2; exit 1; }
[ -f "$HOME/.codex/auth.json" ] || { echo "sign in first: codex login" >&2; exit 1; }

python3 - <<'PY' || { echo; echo "zstd support is missing. Install it with:"; echo "    python3 -m pip install zstandard"; echo "or start Codex with -c features.enable_request_compression=false"; }
try:
    import compression.zstd          # python 3.14+
except ImportError:
    import zstandard                 # pip install zstandard
print("  zstd support: ok")
PY

# ------------------------------------------------------------------- session
if [ -s "$STATE/session.json" ] && [ "${1:-}" != "--resession" ]; then
  echo "  session: reusing $STATE/session.json (re-run with --resession to replace)"
else
  say "Step 1/3 - capture your Prism session"
  cat <<'TXT'
free-astra talks to Prism as you, using your own logged-in browser session.
It needs one real request so it can lift a WARM sandbox token: a token minted
from scratch points at a cold sandbox that boots for minutes and then 504s.

  1. Open your Prism project at https://prism.openai.com and sign in.
  2. Open DevTools > Network.
  3. Send any chat message, e.g. "ping".
  4. Find the POST to /api/llm/response_with_tools_start
  5. Right-click it > Copy > Copy as cURL
  6. Come back here and press Enter.

TXT
  read -r -p "Copied? Press Enter to read it from your clipboard... " _ || true
  python3 "$DIR/scripts/from_curl.py"
  read -r -p "Your Prism project URL (optional, enables auto-refresh): " PURL || true
  if [ -n "${PURL:-}" ]; then
    PURL="$PURL" python3 - <<'PY'
import json, os
p = os.path.join(os.environ.get('FREE_ASTRA_HOME', os.path.expanduser('~/.free-astra')), 'session.json')
d = json.load(open(p, encoding='utf-8')); d['project_url'] = os.environ['PURL']
json.dump(d, open(p, 'w'), indent=2)
PY
  fi
fi

# ------------------------------------------------------------------ manifest
say "Step 2/3 - build the model list from your account"
python3 "$DIR/scripts/build_manifest.py"

# ----------------------------------------------------------------- front door
say "Step 3/3 - wire it into Codex"
"$DIR/front-door.sh" service
"$DIR/front-door.sh" on

say "Checking"
if "$DIR/front-door.sh" status; then :; fi
printf '\n'
cd "$(mktemp -d)"
if codex exec --model prism-astra --skip-git-repo-check \
     "Reply with exactly: FREE-ASTRA-OK" </dev/null 2>/dev/null | grep -q 'FREE-ASTRA-OK'; then
  say "Done. Try it:"
  echo "    codex exec --model prism-astra \"fix the failing test\""
  echo "    codex --model prism-sol"
  echo
  echo "Your normal models are untouched and still work."
  echo "To undo everything:  $DIR/front-door.sh off && $DIR/front-door.sh unservice"
  "$DIR/scripts/star.sh" || true
else
  echo "The smoke test did not pass. Check the log:"
  echo "    tail -30 $STATE/service.log"
  echo "and re-capture the session with: $DIR/install.sh --resession"
  exit 1
fi

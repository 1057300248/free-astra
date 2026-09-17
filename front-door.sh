#!/usr/bin/env bash
# free-astra: put the adapter in front of Codex so prism-* models and every
# normal model coexist in one picker - CLI and the Codex desktop app alike.
#   ./front-door.sh on     point Codex at free-astra (backs up config.toml)
#   ./front-door.sh off    restore the previous openai_base_url
#   ./front-door.sh status
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="$HOME/.codex/config.toml"
STATE="${FREE_ASTRA_HOME:-$HOME/.free-astra}"
mkdir -p "$STATE"
PORT="${PRISM_PORT:-8319}"
MARK="# free-astra front door"

case "${1:-status}" in
on)
  cp "$CFG" "$CFG.pre-free-astra.$(date +%Y%m%d%H%M%S)"
  if ! curl -sf -o /dev/null "http://127.0.0.1:$PORT/v1/models"; then
    (cd "$DIR" && nohup python3 freeastra.py >> "$STATE/server.log" 2>&1 &)
    for _ in $(seq 1 20); do curl -sf -o /dev/null "http://127.0.0.1:$PORT/v1/models" && break; sleep 0.5; done
  fi
  PREV="$(grep -m1 -E '^openai_base_url = ' "$CFG" | sed -E 's/.*"(.*)".*/\1/' || true)"
  if [ -n "$PREV" ] && [ "$PREV" != "http://127.0.0.1:$PORT/v1" ]; then
    PREV="$PREV" python3 - <<'PY2'
import json, os
p = os.path.expanduser(os.environ.get('FREE_ASTRA_HOME', '~/.free-astra')) + '/session.json'
p = os.path.expanduser(p)
if os.path.exists(p):
    d = json.load(open(p, encoding='utf-8'))
    d['upstream'] = os.environ['PREV']          # chain through whatever was here
    json.dump(d, open(p, 'w'), indent=2)
    print('  chaining upstream to %s' % os.environ['PREV'])
PY2
  fi
  CAT="$HOME/.codex/free-astra-catalog.json"
  curl -sf "http://127.0.0.1:$PORT/v1/models?client_version=0.154.0" -o "$CAT.tmp" \
    && python3 -c "
import json,sys
d=json.load(open('$CAT.tmp'))
assert d.get('models'), 'empty catalog'
json.dump(d, open('$CAT','w'), indent=1)
print('  catalog: %d models -> $CAT' % len(d['models']))" \
    && rm -f "$CAT.tmp" \
    || { echo 'could not build the model catalog - is free-astra running?' >&2; exit 1; }

  CFG="$CFG" PORT="$PORT" MARK="$MARK" CAT="$CAT" python3 - <<'PY'
import os, re, shutil, tomllib

def write_checked(path, text):
    bak = path + '.rollback'
    shutil.copy(path, bak)
    open(path, 'w', encoding='utf-8').write(text)
    try:
        tomllib.load(open(path, 'rb'))
    except Exception as e:
        shutil.copy(bak, path)
        raise SystemExit('refusing to write broken config (%s) - rolled back' % e)
    os.remove(bak)
cfg, port, mark = os.environ['CFG'], os.environ['PORT'], os.environ['MARK']
ours = os.environ['CAT']
s = open(cfg, encoding='utf-8').read()
# stash whatever owned the front door before us (opencodex, or nothing)
prev = re.search(r'^openai_base_url = "(.*?)"', s, re.M)
cat = re.search(r'^model_catalog_json = "(.*?)"', s, re.M)
old_cat = cat.group(1) if cat else 'none'
if old_cat == ours:                      # don't record ourselves as the thing to restore
    old_cat = 'none'
lines = ['%s (previous openai_base_url: %s | catalog: %s)'
         % (mark, prev.group(1) if prev else 'none', old_cat),
         'openai_base_url = "http://127.0.0.1:%s/v1"' % port,
         'model_catalog_json = "%s"' % ours]
s = re.sub(r'^# Auto-injected by opencodex\n', '', s, flags=re.M)
s = re.sub(r'^openai_base_url = ".*?"\n', '', s, flags=re.M)
s = re.sub(r'^experimental_realtime_ws_base_url = ".*?"\n', '', s, flags=re.M)
# our /v1/models merges upstream + prism, so a static catalog would mask it
s = re.sub(r'^model_catalog_json = ".*?"\n', '', s, flags=re.M)
s = re.sub(r'^%s.*\n' % re.escape(mark), '', s, flags=re.M)
m2 = re.search(r'^\[', s, re.M)       # first TOML table header, not a '[' in a value
i = m2.start() if m2 else len(s)
s = s[:i] + '\n'.join(lines) + '\n' + s[i:]
write_checked(cfg, s)
print('front door -> http://127.0.0.1:%s/v1' % port)
PY
  ;;
off)
  CFG="$CFG" MARK="$MARK" python3 - <<'PY'
import os, re, shutil, tomllib

def write_checked(path, text):
    bak = path + '.rollback'
    shutil.copy(path, bak)
    open(path, 'w', encoding='utf-8').write(text)
    try:
        tomllib.load(open(path, 'rb'))
    except Exception as e:
        shutil.copy(bak, path)
        raise SystemExit('refusing to write broken config (%s) - rolled back' % e)
    os.remove(bak)
cfg, mark = os.environ['CFG'], os.environ['MARK']
s = open(cfg, encoding='utf-8').read()
m = re.search(r'^%s \(previous openai_base_url: (.*?) \| catalog: (.*?)\)\n' % re.escape(mark), s, re.M)
prev = m.group(1) if m else 'none'
cat = m.group(2) if m else 'none'
s = re.sub(r'^%s.*\n' % re.escape(mark), '', s, flags=re.M)
s = re.sub(r'^openai_base_url = ".*?"\n', '', s, flags=re.M)
back = []
if cat != 'none':
    back.append('model_catalog_json = "%s"' % cat)
if prev != 'none':
    back += ['# Auto-injected by opencodex', 'openai_base_url = "%s"' % prev]
if back:
    m2 = re.search(r'^\[', s, re.M)
    i = m2.start() if m2 else len(s)
    s = s[:i] + '\n'.join(back) + '\n' + s[i:]
write_checked(cfg, s)
print('restored openai_base_url: %s' % prev)
PY
  ;;
service)
  # The desktop Codex app will not start the adapter for us, and with the front
  # door on every Codex request needs it. Keep it alive with a LaunchAgent.
  PLIST="$HOME/Library/LaunchAgents/com.free-astra.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.free-astra</string>
  <key>ProgramArguments</key><array>
    <string>$(command -v python3)</string><string>$DIR/freeastra.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict>
    <key>PRISM_EFFORT</key><string>${PRISM_EFFORT:-medium}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$STATE/service.log</string>
  <key>StandardErrorPath</key><string>$STATE/service.log</string>
</dict></plist>
PL
  launchctl unload "$PLIST" 2>/dev/null || true
  pkill -f 'freeastra.py' 2>/dev/null || true
  sleep 1
  launchctl load "$PLIST"
  echo "service installed: com.free-astra (logs: $STATE/service.log)"
  ;;
unservice)
  PLIST="$HOME/Library/LaunchAgents/com.free-astra.plist"
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "service removed"
  ;;
*)
  grep -E 'openai_base_url|free-astra front door' "$CFG" || echo "front door: off (native)"
  curl -sf -o /dev/null "http://127.0.0.1:$PORT/v1/models" && echo "free-astra: up" || echo "free-astra: down"
  launchctl list 2>/dev/null | grep -q com.free-astra && echo "service: installed" || echo "service: not installed"
  ;;
esac

#!/usr/bin/env bash
# Ask once, after free-astra is confirmed working, whether the user wants to star
# the repo. Never nags: one marker file and it stays quiet forever after.
#
# Skipped entirely when there is no terminal to ask (CI, pipes, launchd), or when
# FREE_ASTRA_NO_PROMPT is set. Nothing is ever starred without an explicit yes.
set -uo pipefail
REPO="Zhao73/free-astra"
STATE="${FREE_ASTRA_HOME:-$HOME/.free-astra}"
MARK="$STATE/.star-prompted"

[ -n "${FREE_ASTRA_NO_PROMPT:-}" ] && exit 0
[ -n "${CI:-}" ] && exit 0
[ -t 0 ] && [ -t 1 ] || exit 0
[ -e "$MARK" ] && exit 0

mkdir -p "$STATE"
: > "$MARK"          # written before asking, so a Ctrl-C never re-prompts either

printf '\n'
printf 'Was free-astra useful to you?\n'
printf 'A star helps other people find it: https://github.com/%s\n' "$REPO"
printf '\n'
read -r -t 30 -p "Star it now? [y/N] " ans || { printf '\nno answer, skipping\n'; exit 0; }

case "${ans:-}" in
  [yY]*) ;;
  *) printf 'No problem. You can star it any time at https://github.com/%s\n' "$REPO"; exit 0;;
esac

if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
  if gh api -X PUT "user/starred/$REPO" --silent 2>/dev/null; then
    printf 'Starred. Thank you.\n'
    exit 0
  fi
  printf 'Could not star via gh.\n'
fi

URL="https://github.com/$REPO"
if command -v open >/dev/null; then open "$URL" >/dev/null 2>&1 || true
elif command -v xdg-open >/dev/null; then xdg-open "$URL" >/dev/null 2>&1 || true
fi
printf 'Opened %s - thank you.\n' "$URL"

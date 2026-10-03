#!/usr/bin/env bash
# Render smoke for the built demo: serves dist/ via `vite preview` and loads
# the page in headless Chrome, failing on (a) missing post-render DOM marker
# or (b) any uncaught console error. Catches runtime JS failures that unit
# tests, `vite build` and curl-based asset checks cannot see (2026-10-03
# blank-page incident: a dropped `const icons` map threw ReferenceError on
# first render while every existing check stayed green).
set -euo pipefail
BASE_PATH="${1:-/gostarbox/}"
URL="http://127.0.0.1:4173${BASE_PATH}"
CHROME="$(command -v google-chrome || command -v chromium || true)"
if [ -z "$CHROME" ]; then
  echo "smoke: no headless chrome found" >&2
  exit 1
fi

npm run preview >/tmp/preview.log 2>&1 &
PREVIEW_PID=$!
trap 'kill "$PREVIEW_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
  curl -sf -o /dev/null "$URL" && break
  sleep 1
done

set +e
DOM="$("$CHROME" --headless=new --no-sandbox --disable-gpu \
  --virtual-time-budget=5000 --enable-logging=stderr --v=0 \
  --dump-dom "$URL" 2>/tmp/chrome.log)"
CHROME_EXIT=$?
set -e

echo "$DOM" > /tmp/dom.html
if [ "$CHROME_EXIT" -ne 0 ]; then
  echo "smoke: chrome exited $CHROME_EXIT"; cat /tmp/chrome.log; exit 1
fi
if ! grep -q 'class="brand"' /tmp/dom.html; then
  echo "smoke: page did not render (no .brand in DOM) — app JS likely threw"; exit 1
fi
if grep -qiE 'uncaught|referenceerror|syntaxerror' /tmp/chrome.log; then
  echo "smoke: console errors during load"; grep -iE 'uncaught|referenceerror|syntaxerror' /tmp/chrome.log; exit 1
fi
echo "smoke: rendered page OK at $URL"

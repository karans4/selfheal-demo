#!/usr/bin/env bash
# Observe a service through the kernel with yeet. Runs inside the yeet Linux VM.
# Usage: observe.sh <service dir> <port> <path>
# Starts the service, drives requests at it, and prints what yeet saw on the wire (JSON lines).
set -u
SVC=$1 PORT=$2 URLPATH=$3
HI=${HTTPINSPECT:-/tmp/httpinspect}
[ -d "$HI" ] || git clone -q --depth 1 https://github.com/yeet-src/httpinspect "$HI"
sed "s/Number(globalThis.TAP_PORT ?? 0)/$PORT/" "$(dirname "$0")/status_tap.js" > "$HI/src/status_tap.js"
sed 's/verify/status_tap/g' "$HI/Makefile" > "$HI/Makefile.tap"
# httpinspect only parses status lines, so it snaps responses to 32 bytes; we want the error body too.
sed -i 's/#define RESP_CAP 32 /#define RESP_CAP 511/' "$HI/src/bpf/httptop.bpf.c"
(cd "$SVC" && exec python3 app.py >/dev/null 2>&1) & SVCPID=$!
(for i in $(seq 1 60); do curl -s -o /dev/null "http://127.0.0.1:$PORT$URLPATH"; sleep 0.25; done) & LOAD=$!
cd "$HI" && timeout 60 make -s -f Makefile.tap status_tap 2>/dev/null | grep '^{'
kill $LOAD $SVCPID 2>/dev/null; wait 2>/dev/null

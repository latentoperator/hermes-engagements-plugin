#!/usr/bin/env bash
# S6b: a separate OS process (stand-in for `hermes engagements ...` CLI or another host)
# holds the shared flock for 4s; the gateway-hosted tool must wait for it.
set -uo pipefail
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
S=$R/.scratch/spike
T=$(date +%s.%N)
python3 -I - "$S/library/probe.lock" <<'EOF' &
import fcntl, os, sys, time
with open(sys.argv[1], "a+") as fh:
    fcntl.flock(fh, fcntl.LOCK_EX)
    print(f"external pid={os.getpid()} holding lock", flush=True)
    time.sleep(4)
EOF
sleep 0.5
(cd "$S/work" && timeout 150 "$R/spike/hx" -p studio-ops chat -q 'CALL engprobe_lock {"hold_s":0}' -Q >/dev/null 2>&1)
wait
python3 - "$S/evidence/probe.jsonl" "$T" <<'EOF'
import json, sys
for line in open(sys.argv[1]):
    r = json.loads(line)
    if r["ts"] >= float(sys.argv[2]) and r["seam"] == "S6.lock":
        print(f"gateway tool pid={r['pid']} profile={r['profile']} waited={r['waited_s']}s")
EOF

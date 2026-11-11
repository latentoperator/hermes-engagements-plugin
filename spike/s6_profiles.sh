#!/usr/bin/env bash
# S6: two profiles share one library_root. Each takes the shared fcntl lock and holds it 4s.
# Pass = the second waits ~the hold time and sees the first one's write; data dirs differ.
set -uo pipefail
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
S=$R/.scratch/spike
HX=$R/spike/hx
T=$(date +%s.%N)
rm -f "$S/library/shared-marker.txt"
run() { (cd "$S/work" && timeout 150 "$HX" -p "$1" chat -q 'CALL engprobe_lock {"hold_s":4}' -Q >"$S/evidence/lock-$1.out" 2>&1); }
run studio-dev &
sleep 1.5
run studio-ops &
wait
python3 - "$S/evidence/probe.jsonl" "$T" <<'EOF'
import json, sys
path, t0 = sys.argv[1], float(sys.argv[2])
for line in open(path):
    r = json.loads(line)
    if r["ts"] < t0 or r["seam"] != "S6.lock":
        continue
    print(f"profile={r['profile']:<11} pid={r['pid']} waited={r['waited_s']}s home=..{r['hermes_home'][-28:]} "
          f"data_dir=..{r['data_dir'][-60:]}\n   seen_before={r['seen_before']!r}")
EOF
echo "--- marker file:"; cat "$S/library/shared-marker.txt"

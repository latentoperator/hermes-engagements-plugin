#!/usr/bin/env bash
# S5: two concurrent sessions in ONE profile (served by one local gateway process).
# Each runs a tool that sleeps, so the calls overlap; then both bind different clients at once.
set -uo pipefail
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
S=$R/.scratch/spike
HX=$R/spike/hx
T=$(date +%s.%N)
run() { (cd "$1" && timeout 150 "$HX" chat -q "$2" -Q >"$3" 2>&1); }
run "$S/work/acme-site" 'CALL engprobe_where {"label":"conc-A","sleep_s":6}' "$S/evidence/concA.out" &
sleep 1
run "$S/work/riverbend-recall" 'CALL engprobe_where {"label":"conc-B","sleep_s":6}' "$S/evidence/concB.out" &
wait
run "$S/work/acme-site" 'CALL engprobe_bind {"client":"acme-bakery"}' "$S/evidence/bindA.out" &
run "$S/work/riverbend-recall" 'CALL engprobe_bind {"client":"riverbend-dental"}' "$S/evidence/bindB.out" &
wait
python3 - "$S/evidence/probe.jsonl" "$T" <<'EOF'
import json, sys
path, t0 = sys.argv[1], float(sys.argv[2])
for line in open(path):
    r = json.loads(line)
    if r["ts"] < t0 or not r["seam"].startswith(("S1", "S4.bind")):
        continue
    print(f"{r['seam']:<14} {r.get('label') or r.get('client'):<17} pid={r['pid']} {r['thread']:<28} "
          f"sid=..{r['session_id'][-8:]} env_sid=..{r['session_env']['HERMES_SESSION_ID'][-8:]} "
          f"cwd={r['session_cwd'].split('/')[-1]:<17} t=+{r['ts'] - t0:.2f}s")
EOF
grep -h -o 'local-[0-9a-f]*' "$S"/evidence/bind[AB].out | sort -u | while read -r sid; do
  echo "binding for ..${sid: -8}: $(python3 -c "import json;print(json.load(open('$S/home/.hermes/plugin-data/agent-plugin-engprobe-fe08eb5c/state.json')).get('bindings',{}).get('$sid'))")"
done

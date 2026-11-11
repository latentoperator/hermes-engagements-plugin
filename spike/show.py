"""Summarize probe evidence: python3 show.py <probe.jsonl> [since_ts] [seam-prefix]."""

import json
import sys

path = sys.argv[1]
since = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
prefix = sys.argv[3] if len(sys.argv) > 3 else ""
with open(path, encoding="utf-8") as fh:
    rows = [json.loads(line) for line in fh]
for r in rows:
    if r["ts"] < since or not r["seam"].startswith(prefix):
        continue
    env = r.get("session_env") or {}
    sid = (r.get("session_id") or "")[-10:]
    print(f"{r['seam']:<22} pid={r['pid']} sid=..{sid} task=..{(r.get('task_id') or '')[-10:]} "
          f"surface={r.get('session_surface')!r} cwd={r.get('session_cwd')!r} "
          f"env_sid=..{(env.get('HERMES_SESSION_ID') or '')[-10:]} "
          f"extra={ {k: r[k] for k in ('label', 'bound', 'migrated', 'old_session_id', 'kind', 'raw_args', 'session_key', 'platform', 'waited_s', 'client', 'after_sleep_session_cwd') if k in r} }")

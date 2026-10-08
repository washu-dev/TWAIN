#!/bin/bash
# RIS inventory (P1, #185): what TWAIN's cluster envs actually contain. READ-ONLY.
#
# Submitted by the cluster monitor (runner/inventory.py) as a short Slurm job;
# runnable by hand too:  TWAIN_ENV_FILE=<twain.sh> bash scripts/ris/inventory.sh
#
# Prints one line, "TWAIN_INVENTORY_JSON: {...}", which the monitor ingests:
#   envs     every <env> under $TWAIN_ENVS_ROOT with a bin/python: the version it
#            points at (.versions/<v>/), its python, and its installed packages --
#            conda's (conda-meta, which also covers non-Python engines like
#            nwchem or ambertools) plus pip's (importlib.metadata)
#   modules  `module -t spider`
# Planning reads these instead of scripts/ris/envs/*.yml: an env that a spec
# promises but nobody provisioned is not a place a job can run (run e825d5ed).
set -uo pipefail
if [ -n "${TWAIN_ENV_FILE:-}" ] && [ -r "$TWAIN_ENV_FILE" ]; then . "$TWAIN_ENV_FILE"; fi
MODS=$(mktemp)
{ module -t spider 2>&1 || true; } | grep -v '^$' > "$MODS"
/usr/bin/env python3 - "${TWAIN_ENVS_ROOT:-}" "$MODS" <<'TWAIN_PY'
import datetime, glob, json, os, re, subprocess, sys

root, mods_file = sys.argv[1], sys.argv[2]
out = {"taken_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
       "host": os.uname().nodename, "envs_root": root or None, "envs": {}, "modules": []}
try:
    out["modules"] = [ln.strip() for ln in open(mods_file) if ln.strip() and not ln.startswith(" ")]
except OSError:
    pass

PIP_LIST = ("import importlib.metadata as m, json\n"
            "print(json.dumps({(d.metadata['Name'] or '').lower(): d.version "
            "for d in m.distributions() if d.metadata['Name']}))")

def norm(name):
    return re.sub(r"[_.]+", "-", name.strip().lower())

if root and os.path.isdir(root):
    for entry in sorted(os.listdir(root)):
        prefix = os.path.join(root, entry)
        python = os.path.join(prefix, "bin", "python")
        if entry.startswith(".") or not os.access(python, os.X_OK):
            continue
        real = os.path.realpath(prefix)
        version = re.search(r"/\.versions/([^/]+)/", real + "/")
        env = {"version": version.group(1) if version else None, "realpath": real,
               "python": None, "packages": {}, "error": None}
        try:
            for meta in glob.glob(os.path.join(prefix, "conda-meta", "*.json")):
                with open(meta) as fh:
                    rec = json.load(fh)
                if rec.get("name"):
                    env["packages"][norm(rec["name"])] = rec.get("version")
            done = subprocess.run([python, "-c", "import sys; print(sys.version.split()[0])"],
                                  capture_output=True, text=True, timeout=60)
            env["python"] = done.stdout.strip() or None
            done = subprocess.run([python, "-c", PIP_LIST], capture_output=True, text=True, timeout=120)
            for name, ver in json.loads(done.stdout or "{}").items():
                env["packages"].setdefault(norm(name), ver)
        except Exception as exc:  # one unreadable env must not hide the others
            env["error"] = f"{type(exc).__name__}: {exc}"[:300]
        out["envs"][entry] = env

print("TWAIN_INVENTORY_JSON: " + json.dumps(out, separators=(",", ":")))
TWAIN_PY
rc=$?
rm -f "$MODS"
exit $rc

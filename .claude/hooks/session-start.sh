#!/bin/bash
# SessionStart hook for Claude Code cloud sessions: installs the toolchain the
# checks in CLAUDE.md need (pixi default + lint envs from pixi.lock, and the
# app's node_modules). Local sessions skip it; use ./dev.sh there.
#
# pixi's own installer (pixi.sh, GitHub releases) is outside the cloud
# environment's default network allowlist, so pixi is bootstrapped from its
# conda-forge package instead, which the package-manager allowlist covers.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

PIXI_VERSION="0.82.0"
PIXI_BUILD="hb5a6893_0"
PIXI_HOME="$HOME/.pixi"
cd "$CLAUDE_PROJECT_DIR"

if ! "$PIXI_HOME/bin/pixi" --version 2>/dev/null | grep -q "$PIXI_VERSION"; then
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  pkg="pixi-${PIXI_VERSION}-${PIXI_BUILD}.conda"
  curl -fsSL -o "$tmp/$pkg" "https://conda.anaconda.org/conda-forge/linux-64/$pkg"
  # A .conda file is a zip holding a zstd tarball; zstandard is from PyPI.
  python3 -m pip install -q --target "$tmp/py" zstandard
  python3 -I - "$tmp/$pkg" "$tmp/out" "$tmp/py" <<'PY'
import glob, sys, tarfile, zipfile
pkg, out, site = sys.argv[1:4]
sys.path.insert(0, site)
import zstandard
zipfile.ZipFile(pkg).extractall(out)
inner = glob.glob(f"{out}/pkg-*.tar.zst")[0]
with zstandard.ZstdDecompressor().stream_reader(open(inner, "rb")) as r:
    tarfile.open(fileobj=r, mode="r|").extractall(f"{out}/root", filter="data")
PY
  mkdir -p "$PIXI_HOME/bin"
  install -m 0755 "$tmp/out/root/bin/pixi" "$PIXI_HOME/bin/pixi"
fi
export PATH="$PIXI_HOME/bin:$PATH"
echo "export PATH=\"$PIXI_HOME/bin:\$PATH\"" >> "${CLAUDE_ENV_FILE:-/dev/null}"

# Default env (pipeline, runner, API tests; includes ruff) and the CI lint env.
# The sim env (GPAW, DFTB+) is left out: no check in CLAUDE.md needs it.
pixi install --locked -e default
pixi install --locked -e lint

# Web app: npm install (not ci) so a cached container reuses node_modules.
(cd app && npm install --no-audit --no-fund)

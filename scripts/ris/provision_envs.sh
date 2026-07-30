#!/usr/bin/env bash
# Provision (or sync) the shared pre-provisioned calculator environments on
# cluster storage from the version-controlled specs in scripts/ris/envs/*.yml.
#
# Run ON a RIS login node (micromamba needs no modules or sudo):
#   bash scripts/ris/provision_envs.sh            # all specs
#   bash scripts/ris/provision_envs.sh default    # just one env
#
# Idempotent: an existing env is synced to its spec (packages added/updated);
# a missing env is created. To add a package for everyone, edit the spec,
# commit, and rerun -- never `micromamba install` into a shared env by hand:
# manual drift is how twain-envs/default silently lost rdkit.
#
# Overridable:
#   TWAIN_TEAM_ROOT  team storage root (default: the compute2 profile's root)
set -euo pipefail

TEAM_ROOT="${TWAIN_TEAM_ROOT:-/storage2/fs1/mdan/Active/dtrc2026-workshop}"
ENVS_ROOT="$TEAM_ROOT/twain-envs"
MAMBA="$TEAM_ROOT/bin/micromamba"
SPECS_DIR="$(cd "$(dirname "$0")/envs" && pwd)"

if [ ! -x "$MAMBA" ]; then
  echo "==> Installing micromamba into $TEAM_ROOT/bin"
  mkdir -p "$TEAM_ROOT/bin"
  curl -fsSL "https://micro.mamba.pm/api/micromamba/linux-64/latest" \
    | tar -xj -C "$TEAM_ROOT" bin/micromamba
fi

specs=()
if [ "$#" -gt 0 ]; then
  for name in "$@"; do specs+=("$SPECS_DIR/$name.yml"); done
else
  specs=("$SPECS_DIR"/*.yml)
fi

for spec in "${specs[@]}"; do
  [ -f "$spec" ] || { echo "ERROR: no spec at $spec" >&2; exit 1; }
  name="$(basename "$spec" .yml)"
  prefix="$ENVS_ROOT/$name"
  if [ -d "$prefix" ]; then
    echo "==> Syncing $name from $(basename "$spec")"
    "$MAMBA" install -y -p "$prefix" -f "$spec"
  else
    echo "==> Creating $name from $(basename "$spec")"
    "$MAMBA" create -y -p "$prefix" -f "$spec"
  fi
  "$prefix/bin/python" -V >/dev/null || {
    echo "ERROR: $prefix has no working python" >&2; exit 1; }
  echo "==> OK: $prefix"
done
echo "==> All envs provisioned."

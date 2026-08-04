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
DATA_ROOT="${TWAIN_DATA_ROOT:-$TEAM_ROOT/twain-data}"
MAMBA="$TEAM_ROOT/bin/micromamba"
SPECS_DIR="$(cd "$(dirname "$0")/envs" && pwd)"

# Teach an env where its parameter/pseudopotential data lives.
#
# conda-forge ships these engines as bare binaries: DFTB+ needs Slater-Koster
# files, Quantum ESPRESSO and ABINIT need pseudopotentials, and each looks them
# up through its own environment variable. Rather than teach every execution
# adapter about every engine, we write a conda activate.d hook -- because the
# Slurm payload's twain_use_env() already sources activate.d/*.sh (that is how
# NWChem finds NWCHEM_BASIS_LIBRARY), so the cluster, a local `micromamba
# activate`, and the docker adapter all pick this up for free.
#
# Fetch the data itself with scripts/ris/fetch_data.sh.
write_data_hook() {  # <env-name> <prefix>
  local name="$1" prefix="$2" hook_dir="$2/etc/conda/activate.d"
  local var="" val="" manifest="" command_var="" command_val=""
  case "$name" in
    dftbplus)
      # Trailing slash is required: DFTB+ concatenates DFTB_PREFIX with the
      # filename, so without it the path becomes ...slakoC-C.skf.
      var="DFTB_PREFIX"; val="$DATA_ROOT/slako/"
      # Without DFTB_COMMAND, ASE 3.29 falls back to an ase config profile that
      # does not exist here.
      command_var="DFTB_COMMAND"; command_val="dftb+" ;;
    qe)
      var="ESPRESSO_PSEUDO"; val="$DATA_ROOT/sssp"
      manifest="$DATA_ROOT/sssp/SSSP_1.3.0_PBE_efficiency.json" ;;
    abinit)
      var="ABINIT_PP_PATH"; val="$DATA_ROOT/pseudodojo"
      manifest="$DATA_ROOT/pseudodojo/standard.djson" ;;
    cp2k)
      # CP2K is the exception: its data ships inside the conda package, so this
      # points into the env rather than at team storage.
      var="CP2K_DATA_DIR"; val="$prefix/share/cp2k/data"
      # ASE 3.29 defaults to `cp2k.psmp -s`, which does NOT exist in the
      # glibc-pinned 2024.2 nompi build -- its only binary is cp2k.ssmp. Left to
      # the default, the shell subprocess never comes up and ASE reports the
      # opaque "Did not receive * READY after starting CP2K shell".
      command_var="ASE_CP2K_COMMAND"; command_val="cp2k.ssmp -s" ;;
    *) return 0 ;;
  esac

  if [ ! -e "$val" ]; then
    echo "WARNING: $name's data dir is missing: $val" >&2
    if [ "$name" = "cp2k" ]; then
      echo "         expected it inside the conda package -- check the build" >&2
    else
      echo "         run scripts/ris/fetch_data.sh to populate it" >&2
    fi
  fi

  mkdir -p "$hook_dir"
  {
    echo "#!/bin/sh"
    echo "# Written by scripts/ris/provision_envs.sh -- do not hand-edit."
    echo "export $var=\"$val\""
    [ -n "$command_var" ] && echo "export $command_var=\"$command_val\""
    # TWAIN_PSEUDO_MANIFEST is how the bundle's twain_pseudo.py finds the
    # element -> filename + cutoff table, so generated code never spells a
    # pseudopotential filename (or an energy cutoff) itself.
    [ -n "$manifest" ] && echo "export TWAIN_PSEUDO_MANIFEST=\"$manifest\""
    # Keep the block's own exit status 0. The two conditional echoes above are
    # evaluated HERE, at provision time, and for an env with no manifest the last
    # one is false -- which would make this `{ ...; } > file` group return 1 and,
    # under `set -e`, abort provisioning right after a successful env build.
    echo "true"
  } > "$hook_dir/twain_data.sh"
  chmod +x "$hook_dir/twain_data.sh"
  echo "==> data hook: $var=$val${command_var:+, $command_var=$command_val}"
}

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
  write_data_hook "$name" "$prefix"
  echo "==> OK: $prefix"
done
echo "==> All envs provisioned."

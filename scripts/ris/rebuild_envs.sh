#!/usr/bin/env bash
# Versioned rebuilds of the shared RIS envs: build beside, verify, promote by symlink.
#
# Shared envs are used by every TWAIN simulation, so they are never modified in
# place. A rebuild creates a new version next to the live one, checks it, and
# only then re-points the env's name at it -- rollback is re-pointing back.
#
#   $TWAIN_ENVS_ROOT/
#     .versions/<version>/<env>   real conda prefixes (built here, never edited)
#     <env> -> .versions/<version>/<env>   what jobs use ($TWAIN_ENVS_ROOT/<env>/bin/python)
#     .retired-<stamp>/<env>      whatever <env> was before a promote (dir or link)
#
# Subcommands (each takes env names; none = all specs in scripts/ris/envs/):
#   build    <version> [env...]   provision_envs.sh into .versions/<version>/<env>
#   verify   <version> [env...]   imports + engine binaries + no foreign prefixes + ACLs
#   promote  <version> [env...]   verify, then <env> -> .versions/<version>/<env>
#   rollback <env> <version>      re-point <env> at an older version
#   status                        what each <env> points at, and the versions on disk
#
# Builds must happen AT their final path (conda prefixes are path-dependent),
# which is why versions live under .versions/ and only the symlink moves.
#
# Run as the TWAIN account with twain.sh sourced (TWAIN_HOME, TWAIN_ENVS_ROOT,
# CODE_DIR). Inside a Slurm job (general-short: 30 min cap, one env per job) or
# on a login node. Changing a shared env is an approved change (#187).
set -uo pipefail
umask 022

: "${TWAIN_HOME:?source twain.sh first}"
: "${TWAIN_ENVS_ROOT:?source twain.sh first}"
export TWAIN_TEAM_ROOT="$TWAIN_HOME"
export MAMBA_ROOT_PREFIX="${MAMBA_ROOT_PREFIX:-$TWAIN_HOME/.micromamba}"  # off the home quota
# Copy, never hard-link, package files into a shared env. conda hard-links env
# files to its package cache by default, so ANY later write into an env (a
# `cp -a` over it, a stray pip) silently rewrites the cache -- and every env
# built from it afterwards. That poisoned the cache on 2026-10-07: fresh builds
# came out with files from the old dtrc2026-workshop tree.
export MAMBA_ALWAYS_COPY=true
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPECS_DIR="$HERE/envs"
E="$TWAIN_ENVS_ROOT"

# What each env must provide, beyond a working python (kept beside the specs'
# intent; the smoke test in a real job is still the final word).
imports_for() {
  case "$1" in
    default) echo "ase rdkit xtb pymatgen" ;;
    gpaw)    echo "ase gpaw pymatgen" ;;
    nwchem)  echo "ase rdkit openff.toolkit openmm pymatgen" ;;
    psi4)    echo "ase rdkit psi4" ;;
    *)       echo "ase rdkit pymatgen" ;;
  esac
}
# A real calculation step per env, where an import isn't proof enough: run the
# capability plans actually depend on (empty = imports + binaries only).
functional_check_for() {
  case "$1" in
    # AM1-BCC charges: what OpenFF/Interchange does for every small molecule.
    nwchem) echo "from openff.toolkit import Molecule; m = Molecule.from_smiles('CCO'); m.assign_partial_charges('am1bcc'); print('am1bcc OK', round(float(sum(m.partial_charges.m)), 6))" ;;
    *) echo ;;
  esac
}
binaries_for() {
  case "$1" in
    abinit) echo abinit ;; cp2k) echo cp2k.ssmp ;; dftbplus) echo "dftb+" ;;
    nwchem) echo "nwchem sqm" ;; psi4) echo psi4 ;; qe) echo pw.x ;; *) echo ;;
  esac
}

envs_or_all() {
  if [ "$#" -gt 0 ]; then echo "$@"; else
    for s in "$SPECS_DIR"/*.yml; do basename "$s" .yml; done | tr '\n' ' '; fi
}

build() {
  local version="$1"; shift
  for name in $(envs_or_all "$@"); do
    local p="$E/.versions/$version/$name"
    if [ -e "$p" ]; then echo "[build] $p exists -- versions are never overwritten"; return 5; fi
    mkdir -p "$E/.versions/$version"
    # One package cache per build: parallel builds (one Slurm job per env)
    # never share -- or corrupt -- each other's cache. Disposable afterwards.
    # A per-build root prefix makes micromamba's default cache ($root/pkgs) fresh
    # whichever cache variable it honours.
    local root="$MAMBA_ROOT_PREFIX/builds/$version-$name"
    echo "[build] $name -> $p (package cache $root/pkgs)"
    MAMBA_ROOT_PREFIX="$root" MAMBA_PKGS_DIRS="$root/pkgs" CONDA_PKGS_DIRS="$root/pkgs" \
      TWAIN_ENVS_ROOT="$E/.versions/$version" bash "$HERE/provision_envs.sh" "$name" || return 1
    chmod -R go-w "$p"
    # The directories above a version matter as much as the version: whoever can
    # write .versions/ can swap a version out from under its symlink.
    chmod go-w "$E" "$E/.versions" "$E/.versions/$version"
  done
}

# Run a command inside an env the way a TWAIN job does (twain_use_env in the
# Slurm payload): its bin first on PATH, CONDA_PREFIX set, activate.d hooks
# sourced. Checking with a bare "$p/bin/python" lied: OpenFF finds AmberTools
# by looking for sqm on PATH, so AM1-BCC "failed" in an env where jobs work.
in_env() {  # <prefix> <command...>
  local prefix="$1"; shift
  ( export PATH="$prefix/bin:$PATH" CONDA_PREFIX="$prefix"
    for hook in "$prefix"/etc/conda/activate.d/*.sh; do
      [ -r "$hook" ] && . "$hook" >/dev/null 2>&1
    done
    "$@" )
}

verify_one() {  # <version> <env>; prints [verify] lines, returns non-zero on any failure
  local version="$1" name="$2" p="$E/.versions/$1/$2" fail=0 m b n first broad
  [ -x "$p/bin/python" ] || { echo "[verify] $name: no python at $p"; return 1; }
  for m in $(imports_for "$name"); do
    if err=$(in_env "$p" python -c "import $m" 2>&1); then echo "[verify] $name import $m OK"
    else echo "[verify] $name import $m FAILED: $(echo "$err" | tail -1)"; fail=1; fi
  done
  for b in $(binaries_for "$name"); do
    [ -x "$p/bin/$b" ] && echo "[verify] $name binary $b OK" || { echo "[verify] $name binary $b MISSING"; fail=1; }
  done
  local check; check=$(functional_check_for "$name")
  if [ -n "$check" ]; then
    if err=$(in_env "$p" python -c "$check" 2>&1); then echo "[verify] $name functional: $(echo "$err" | tail -1)"
    else echo "[verify] $name functional check FAILED: $(echo "$err" | tail -1)"; fail=1; fi
  fi
  # A file naming another prefix (a copied env, an old team root) runs code from
  # there -- or came from a poisoned package cache. Neither may ship.
  n=$(grep -rIl -e "/twain-envs/" "$p/bin" "$p/etc" 2>/dev/null | xargs -r grep -L "$p" 2>/dev/null | wc -l)
  [ "$n" -eq 0 ] && echo "[verify] $name no foreign prefixes" \
    || { echo "[verify] $name $n file(s) name another env prefix"; fail=1; }
  n=$(grep -rIl "dtrc2026-workshop" "$p" 2>/dev/null | wc -l)
  [ "$n" -eq 0 ] && echo "[verify] $name no dtrc2026-workshop content" \
    || { echo "[verify] $name $n file(s) carry dtrc2026-workshop, e.g. $(grep -rIl dtrc2026-workshop "$p" | head -1)"; fail=1; }
  n=$(find "$p" -type f -links +1 2>/dev/null | head -50 | wc -l)
  [ "$n" -eq 0 ] && echo "[verify] $name no hard-linked files" \
    || { echo "[verify] $name has hard-linked files (shared with a cache or another env)"; fail=1; }
  first=$(grep -m1 '# cmd' "$p/conda-meta/history" 2>/dev/null)
  echo "$first" | grep -q -- "$p" && echo "[verify] $name history starts with its own build" \
    || { echo "[verify] $name history starts elsewhere: $first"; fail=1; }
  # storage2 is NFSv4: mode bits lie, the ACL decides. Nothing may let EVERYONE@
  # or domain users (gid 1000070) write.
  if command -v nfs4_getfacl >/dev/null; then
    broad=$(for f in "$E" "$E/.versions" "$E/.versions/$version" "$p" "$p/bin" "$p/bin/python" \
                     $(find "$p" -type f | shuf -n 300); do
      nfs4_getfacl "$f" 2>/dev/null | grep -qE '^A:[a-zA-Z]*:(EVERYONE@|1000070):[^:]*[waD][^:]*$' && echo "$f"
    done | head -3)
    [ -z "$broad" ] && echo "[verify] $name no broad write ACEs" || { echo "[verify] $name broad write: $broad"; fail=1; }
  fi
  [ "$fail" -eq 0 ] && echo "[verify] PASS $name" || echo "[verify] FAIL $name"
  return "$fail"
}

verify() {
  local version="$1" rc=0; shift
  for name in $(envs_or_all "$@"); do verify_one "$version" "$name" || rc=1; done
  return "$rc"
}

promote() {
  local version="$1" stamp; shift
  stamp=$(date +%Y%m%d%H%M%S)
  for name in $(envs_or_all "$@"); do
    verify_one "$version" "$name" >/dev/null || { echo "[promote] $name@$version fails verify; not promoted"; continue; }
    if [ -e "$E/$name" ] || [ -L "$E/$name" ]; then
      mkdir -p "$E/.retired-$stamp"
      mv "$E/$name" "$E/.retired-$stamp/$name"
    fi
    ln -s ".versions/$version/$name" "$E/$name"
    echo "[promote] $name -> $(readlink "$E/$name")$( [ -e "$E/.retired-$stamp/$name" ] && echo "  (previous kept in .retired-$stamp/$name)")"
  done
}

rollback() {
  local name="$1" version="$2"
  [ -x "$E/.versions/$version/$name/bin/python" ] || { echo "[rollback] no $name in version $version"; return 1; }
  ln -sfn ".versions/$version/$name" "$E/$name"
  echo "[rollback] $name -> $(readlink "$E/$name")"
}

status() {
  for s in "$SPECS_DIR"/*.yml; do
    name=$(basename "$s" .yml)
    if [ -L "$E/$name" ]; then echo "$name -> $(readlink "$E/$name")"
    elif [ -d "$E/$name" ]; then echo "$name: unversioned directory"
    else echo "$name: missing"; fi
  done
  echo "versions: $(ls "$E/.versions" 2>/dev/null | tr '\n' ' ')"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  build|verify|promote) [ "$#" -ge 1 ] || { echo "usage: $0 $cmd <version> [env...]"; exit 2; }; "$cmd" "$@" ;;
  rollback) [ "$#" -eq 2 ] || { echo "usage: $0 rollback <env> <version>"; exit 2; }; rollback "$@" ;;
  status) status ;;
  *) sed -n '2,26p' "$0"; exit 2 ;;
esac

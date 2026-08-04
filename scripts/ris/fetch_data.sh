#!/usr/bin/env bash
# Fetch the parameter/pseudopotential libraries the external engines need onto
# shared cluster storage, next to the pre-provisioned envs.
#
# Run ON a RIS login node (needs only curl, tar, git -- no modules, no sudo):
#   bash scripts/ris/fetch_data.sh              # everything that is missing
#   bash scripts/ris/fetch_data.sh sssp         # just one set
#   bash scripts/ris/fetch_data.sh --force      # refetch even if present
#
# Why this exists: conda-forge ships these engines as bare binaries. DFTB+ needs
# a Slater-Koster set per element pair, and Quantum ESPRESSO / ABINIT need a
# pseudopotential per element -- none of which are in the package. Without them a
# run dies on its first energy evaluation ("SK file ... not found", "cannot open
# file ...UPF"), which reads like a broken install rather than missing data.
#
# Every set is version-pinned and, where the publisher documents a checksum,
# verified against it: these files ARE the physics, so a silently different
# pseudopotential is a silently different answer.
#
# Layout (TWAIN_DATA_ROOT, exported into each env by provision_envs.sh):
#   twain-data/slako/        .skf pairs               -> DFTB_PREFIX
#   twain-data/sssp/         .UPF + SSSP manifest     -> ESPRESSO_PSEUDO
#   twain-data/pseudodojo/   .psp8 + standard.djson   -> ABINIT_PP_PATH
#
# Idempotent: a set whose representative file is already present is skipped.
set -euo pipefail

TEAM_ROOT="${TWAIN_TEAM_ROOT:-/storage2/fs1/mdan/Active/dtrc2026-workshop}"
DATA_ROOT="${TWAIN_DATA_ROOT:-$TEAM_ROOT/twain-data}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── pinned versions (bump deliberately) ──────────────────────────────────────
# SSSP: the efficiency variant, which is what you want when a plan asks for a
# property rather than a convergence study. Published on the Materials Cloud
# Archive with per-file MD5s; the record id is stable across versions.
SSSP_VERSION="1.3.0"
SSSP_RECORD="rcyfm-68h65"
SSSP_TARBALL_MD5="a58f1b3373f330179fd0832c48bb9a52"
SSSP_JSON_MD5="3153c4b20fc90a44fba0236627525644"
# PseudoDojo: ONCVPSP-PBE-PDv0.4, the standard norm-conserving scalar-relativistic
# table. Pinned to a commit rather than a tag -- the repo's only tag (v0.3)
# predates the PDv0.4 table entirely.
DOJO_COMMIT="4048962957711c04281ffe6c2bd2c96f0f7110aa"
DOJO_TABLE="ONCVPSP-PBE-PDv0.4"

FORCE=""
sets=()
for arg in "$@"; do
  case "$arg" in
    --force) FORCE="--force" ;;
    *) sets+=("$arg") ;;
  esac
done
[ "${#sets[@]}" -gt 0 ] || sets=(slako sssp pseudodojo)

log() { echo "[fetch-data] $*"; }

# Verify a file against a published MD5. A mismatch is fatal: a truncated or
# swapped pseudopotential would otherwise become a wrong number in a report.
check_md5() {  # <file> <expected>
  local actual
  actual="$(md5sum "$1" | cut -d' ' -f1)"
  if [ "$actual" != "$2" ]; then
    echo "ERROR: checksum mismatch for $1" >&2
    echo "  expected $2" >&2
    echo "  actual   $actual" >&2
    return 1
  fi
  log "  checksum OK: $(basename "$1")"
}

# Post-condition: every element the manifest names must actually be on disk.
#
# A verified tarball checksum says the download was intact; it says nothing about
# the extraction. The first version of this script copied with `-name '*.UPF'`
# and, because half of SSSP's filenames end in lowercase `.upf`, silently landed
# 53 of 103 elements -- Ag, Au, Cu and Ar among the missing. Nothing failed. A
# later run would simply have been unable to treat copper. So the manifest is now
# checked against the directory, and a gap is fatal here rather than a puzzle
# three hours into a queue.
verify_manifest() {  # <dir> <manifest-filename> <sssp|dojo>
  python3 - "$1" "$2" "$3" <<'PY'
import json
import os
import sys

directory, manifest_name, kind = sys.argv[1], sys.argv[2], sys.argv[3]
with open(os.path.join(directory, manifest_name)) as fh:
    manifest = json.load(fh)
if kind == "dojo":
    manifest = manifest["pseudos_metadata"]
    names = {el: meta["basename"] for el, meta in manifest.items()}
else:
    names = {el: meta["filename"] for el, meta in manifest.items()}

on_disk = set(os.listdir(directory))
missing = sorted((el, fn) for el, fn in names.items() if fn not in on_disk)
print(f"[fetch-data]   manifest: {len(names)} elements, "
      f"{len(names) - len(missing)} present, {len(missing)} missing")
if missing:
    print(f"ERROR: {len(missing)} element(s) named by {manifest_name} are not in "
          f"{directory}:", file=sys.stderr)
    for el, fn in missing[:15]:
        print(f"  {el:<3} -> {fn}", file=sys.stderr)
    if len(missing) > 15:
        print(f"  ... and {len(missing) - 15} more", file=sys.stderr)
    sys.exit(1)
PY
}

fetch_slako() {
  local dest="$DATA_ROOT/slako"
  # Delegate to the existing fetcher so the pinned mio/tiorg versions live in
  # exactly one place; SLAKO_DEST redirects it here from its default repo dir.
  log "slako -> $dest (via runner/fetch_slako.sh)"
  SLAKO_DEST="$dest" bash "$REPO_ROOT/runner/fetch_slako.sh" ${FORCE:+--force}
}

fetch_sssp() {
  local dest="$DATA_ROOT/sssp"
  local json="SSSP_${SSSP_VERSION}_PBE_efficiency.json"
  local tarball="SSSP_${SSSP_VERSION}_PBE_efficiency.tar.gz"
  local base="https://archive.materialscloud.org/records/$SSSP_RECORD/files"
  if [ -z "$FORCE" ] && [ -f "$dest/$json" ] && [ -n "$(find "$dest" -iname '*.upf' -print -quit 2>/dev/null)" ]; then
    log "sssp already present at $dest (use --force to refetch)"
    return 0
  fi
  mkdir -p "$dest"
  local tmp
  tmp="$(mktemp -d)"
  # shellcheck disable=SC2064  # expand tmp now, not at trap time
  trap "rm -rf '$tmp'" RETURN

  log "sssp $SSSP_VERSION -> $dest"
  # The archive 302s to a signed object-store URL, hence -L.
  curl -fsSL --max-time 600 "$base/$json?download=1" -o "$tmp/$json"
  check_md5 "$tmp/$json" "$SSSP_JSON_MD5"
  curl -fsSL --max-time 1800 "$base/$tarball?download=1" -o "$tmp/$tarball"
  check_md5 "$tmp/$tarball" "$SSSP_TARBALL_MD5"

  # The tarball is a flat set of pseudopotential files; keep them flat so
  # ESPRESSO_PSEUDO (and ASE's pseudo_dir, which does not recurse) points
  # straight at them. -iname, not -name: SSSP mixes `.UPF` and `.upf` and a
  # case-sensitive glob silently drops half the periodic table.
  tar -xzf "$tmp/$tarball" -C "$tmp"
  find "$tmp" -iname '*.upf' -exec cp -t "$dest" {} +
  cp "$tmp/$json" "$dest/$json"
  verify_manifest "$dest" "$json" sssp

  cat > "$dest/ATTRIBUTION.md" <<EOF
# SSSP pseudopotentials (fetched, not committed)

Standard Solid State Pseudopotentials, **efficiency** variant, PBE, v$SSSP_VERSION,
downloaded by \`scripts/ris/fetch_data.sh\` from the Materials Cloud Archive
(record \`$SSSP_RECORD\`) and verified against the published MD5s.

Cite: G. Prandini, A. Marrazzo, I. E. Castelli, N. Mounet, N. Marzari,
*npj Computational Materials* **4**, 72 (2018); DOI 10.24435/materialscloud:f3-ym.

\`$json\` maps each element to its recommended file **and** its recommended
\`cutoff_wfc\`/\`cutoff_rho\` -- read it rather than guessing either.
EOF
}

fetch_pseudodojo() {
  local dest="$DATA_ROOT/pseudodojo"
  if [ -z "$FORCE" ] && [ -f "$dest/standard.djson" ] && [ -n "$(find "$dest" -iname '*.psp8' -print -quit 2>/dev/null)" ]; then
    log "pseudodojo already present at $dest (use --force to refetch)"
    return 0
  fi
  mkdir -p "$dest"
  local tmp
  tmp="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" RETURN

  log "pseudodojo $DOJO_TABLE @ ${DOJO_COMMIT:0:7} -> $dest"
  # Sparse + treeless clone: the full history carries every table ever shipped
  # (GBs); we want one directory at one commit.
  git -c advice.detachedHead=false clone -q --filter=blob:none --no-checkout \
    https://github.com/abinit/pseudo_dojo.git "$tmp/repo"
  git -C "$tmp/repo" sparse-checkout set --no-cone "pseudo_dojo/pseudos/$DOJO_TABLE"
  git -C "$tmp/repo" -c advice.detachedHead=false checkout -q "$DOJO_COMMIT"

  local table="$tmp/repo/pseudo_dojo/pseudos/$DOJO_TABLE"
  [ -d "$table" ] || { echo "ERROR: $DOJO_TABLE missing at $DOJO_COMMIT" >&2; return 1; }
  # Upstream stores one directory per element. Flatten: ASE passes ABINIT a list
  # of search paths (AbinitProfile(pp_paths=...)) and 86 per-element paths is not
  # a usable list, whereas one flat directory is.
  find "$table" -iname '*.psp8' -exec cp -t "$dest" {} +
  cp "$table/standard.djson" "$dest/standard.djson"
  verify_manifest "$dest" standard.djson dojo

  cat > "$dest/ATTRIBUTION.md" <<EOF
# PseudoDojo pseudopotentials (fetched, not committed)

$DOJO_TABLE, the standard norm-conserving scalar-relativistic table, taken from
\`abinit/pseudo_dojo\` at commit \`$DOJO_COMMIT\` by
\`scripts/ris/fetch_data.sh\` and flattened into this one directory.

Cite: M. J. van Setten et al., *Computer Physics Communications* **226**, 39 (2018).

\`standard.djson\` maps each element to its \`basename\` **and** its recommended
\`hints.{low,normal,high}.ecut\` -- read it rather than guessing either.
EOF
}

mkdir -p "$DATA_ROOT"
for name in "${sets[@]}"; do
  case "$name" in
    slako) fetch_slako ;;
    sssp) fetch_sssp ;;
    pseudodojo) fetch_pseudodojo ;;
    *) echo "ERROR: unknown data set '$name' (slako|sssp|pseudodojo)" >&2; exit 1 ;;
  esac
done
log "all requested data present under $DATA_ROOT"

#!/usr/bin/env bash
# Fetch DFTB+ Slater-Koster (.skf) parameter files into <repo>/slako/.
#
# DFTB+ ships only the binary; it needs a separate .skf parameter set for every
# element pair it encounters, found via the DFTB_PREFIX environment variable
# (wired to <repo>/slako/ by pixi's [activation.env]). Without them a real run
# aborts with "SK file ... not found" (exit 1) on the first energy evaluation.
#
# We pull two published, CC-BY-SA sets from the dftbparams GitHub org (no login,
# unlike dftb.org) and flatten them into one prefix directory:
#   - mio    (H,C,N,O,S,P)      biological/organic SCC-DFTB  -> supplies O-O, C-C, ...
#   - tiorg  (adds Ti pairs)    bulk Ti / TiO2 / surfaces    -> supplies Ti-Ti, Ti-O, O-Ti
# tiorg is designed to be used alongside mio, and their file sets don't overlap
# (tiorg is only Ti-* pairs, mio has no Ti), so the union is a coherent set that
# covers organics plus rutile/anatase TiO2.
#
# Usage:  bash runner/fetch_slako.sh [--force]
#   --force  refetch even if the files are already present (idempotent otherwise)
set -euo pipefail

# Pinned for reproducibility (bump deliberately; see the repos' CHANGELOGs).
MIO_VERSION="v1.1.0"
TIORG_VERSION="v0.1.0"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# SLAKO_DEST lets the cluster fetcher (scripts/ris/fetch_data.sh) put the same
# pinned sets on shared team storage instead of in a per-checkout slako/ dir, so
# the pinned versions above stay the single source of truth for both.
DEST="${SLAKO_DEST:-$REPO_ROOT/slako}"
FORCE="${1:-}"

# Idempotent: skip if a representative file from each set is already there.
if [[ "$FORCE" != "--force" && -f "$DEST/Ti-O.skf" && -f "$DEST/O-O.skf" ]]; then
  echo "[fetch-slako] already present at $DEST (use --force to refetch)"
  exit 0
fi

mkdir -p "$DEST"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fetch_set() {  # <repo-name> <tag>
  local name="$1" version="$2"
  local url="https://github.com/dftbparams/$name/archive/refs/tags/$version.tar.gz"
  echo "[fetch-slako] downloading $name $version"
  curl -fsSL "$url" -o "$TMP/$name.tar.gz"
  tar -xzf "$TMP/$name.tar.gz" -C "$TMP"
  # GitHub strips the leading 'v' from the tag for the top-level dir name, so
  # match by glob rather than hard-coding "$name-$version".
  cp "$TMP/$name-"*/skfiles/*.skf "$DEST"/
  # Keep each set's LICENSE + README next to the data (CC-BY-SA attribution).
  cp "$TMP/$name-"*/LICENSE "$DEST/LICENSE.$name"
  cp "$TMP/$name-"*/README "$DEST/README.$name" 2>/dev/null || true
}

fetch_set mio "$MIO_VERSION"
fetch_set tiorg "$TIORG_VERSION"

cat > "$DEST/ATTRIBUTION.md" <<EOF
# Slater-Koster parameter files (fetched, not committed)

Downloaded by \`runner/fetch_slako.sh\` from the dftbparams GitHub org and
combined into this single DFTB_PREFIX directory:

- **mio** $MIO_VERSION  — https://github.com/dftbparams/mio  (H, C, N, O, S, P)
- **tiorg** $TIORG_VERSION — https://github.com/dftbparams/tiorg  (adds Ti; bulk Ti / TiO2 / surfaces)

Both sets are licensed **CC-BY-SA 4.0**; see \`LICENSE.mio\` / \`LICENSE.tiorg\`.
This directory is gitignored — regenerate with \`pixi run fetch-slako\`.
EOF

count=$(find "$DEST" -name '*.skf' | wc -l | tr -d ' ')
echo "[fetch-slako] done: $count .skf files in $DEST"

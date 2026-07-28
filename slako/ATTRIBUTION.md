# Slater-Koster parameter files (fetched, not committed)

Downloaded by `runner/fetch_slako.sh` from the dftbparams GitHub org and
combined into this single DFTB_PREFIX directory:

- **mio** v1.1.0  — https://github.com/dftbparams/mio  (H, C, N, O, S, P)
- **tiorg** v0.1.0 — https://github.com/dftbparams/tiorg  (adds Ti; bulk Ti / TiO2 / surfaces)

Both sets are licensed **CC-BY-SA 4.0**; see `LICENSE.mio` / `LICENSE.tiorg`.
This directory is gitignored — regenerate with `pixi run fetch-slako`.

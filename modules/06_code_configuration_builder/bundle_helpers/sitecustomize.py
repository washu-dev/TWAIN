"""Loaded by Python at start-up when the bundle is on PYTHONPATH (the job payload
puts it there): installs TWAIN's structure guard (twain_structure_guard.py).
Must never stop the interpreter from starting."""
try:
    import twain_structure_guard as _twain_guard

    _twain_guard.install()
except Exception as _exc:  # noqa: BLE001
    import sys as _sys
    print(f"[twain-guard] not installed: {_exc}", file=_sys.stderr)

"""The API's release version (#173): ``YYYY.MM.DD.NNN``, independent of the app's.

Resolution: ``TWAIN_VERSION`` (baked into the image by the deploy, which
computed it once with scripts/bump-version.sh) -> ``api/VERSION`` (a checkout)
-> ``"dev"``. Read per call, not at import, so tests can set the variable.
"""
import os
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent / "VERSION"


def get_version() -> str:
    env = os.getenv("TWAIN_VERSION", "").strip()
    if env:
        return env
    try:
        text = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "dev"
    # 0000.00.00.000 is the never-released placeholder.
    return text if text and not text.startswith("0000.") else "dev"

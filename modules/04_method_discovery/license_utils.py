"""License classification helpers shared by the external discovery adapters.

Maps an SPDX-ish license identifier to the coarse `LicenseClass` used by the
scoring rubric (Story 4.2). Anything unrecognised is treated as `restrictive`
so unknown licenses score conservatively until vetted.
"""

from method_discovery.registry_loader import LicenseClass

_PERMISSIVE_PREFIXES = (
    "MIT",
    "BSD",
    "APACHE",
    "ISC",
    "ZLIB",
    "PYTHON",
    "PSF",
    "UNLICENSE",
    "0BSD",
    "WTFPL",
    "BOOST",
    "BSL",
)

_COPYLEFT_PREFIXES = (
    "GPL",
    "LGPL",
    "AGPL",
    "MPL",
    "EPL",
    "CDDL",
    "CECILL",
    "OSL",
    "EUPL",
)


def classify_license(spdx: str) -> LicenseClass:
    """Classify a license string as permissive, copyleft, or restrictive.

    The check is case-insensitive and prefix-based so variants like
    'Apache-2.0', 'Apache 2.0', 'GPL-3.0-only', or 'LGPLv3' are handled.
    """
    if not spdx or type(spdx) is not str:
        return LicenseClass.RESTRICTIVE
    token = spdx.strip().upper()
    if token in ("", "NOASSERTION", "UNKNOWN", "OTHER", "PROPRIETARY"):
        return LicenseClass.RESTRICTIVE

    normalized = token.replace("_", "-").replace(" ", "-")
    # Copyleft is checked first: 'LGPL' must not be caught by a 'GPL' substring,
    # but prefix checks on the normalized token handle both correctly.
    for prefix in _COPYLEFT_PREFIXES:
        if normalized.startswith(prefix):
            return LicenseClass.COPYLEFT
    for prefix in _PERMISSIVE_PREFIXES:
        if normalized.startswith(prefix):
            return LicenseClass.PERMISSIVE
    return LicenseClass.RESTRICTIVE

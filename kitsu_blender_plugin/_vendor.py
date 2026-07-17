"""Makes the bundled third-party libraries under vendor/ importable.

Blender's bundled Python does not ship `gazu` (CGWire's official Kitsu/Zou
API client) or the packages it needs at runtime (`requests`, `urllib3`,
`certifi`, `idna`), so copies of all of them are vendored here. This module
must be imported before anything does `import gazu` or `import requests`.
"""

import os
import sys

_VENDOR_DIR = os.path.join(os.path.dirname(__file__), "vendor")

if _VENDOR_DIR in sys.path:
    sys.path.remove(_VENDOR_DIR)

# Inserted at index 0, not appended: this plugin is only tested against the
# exact gazu/requests/urllib3/etc. versions vendored here, so they must
# always win over anything already importable elsewhere on sys.path — a
# stale pip install into Blender's own Python, a studio PYTHONPATH entry,
# another addon's own vendored copy, etc. An older gazu shadowing ours this
# way previously surfaced (in this plugin's Maya counterpart) as a confusing
# "module 'gazu.user' has no attribute '...'" error for a method that does
# exist in the version actually vendored here — the vendored copy is
# authoritative, so it must be found first.
sys.path.insert(0, _VENDOR_DIR)

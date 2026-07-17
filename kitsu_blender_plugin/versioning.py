"""Parses a version number out of a Blender filename.

Matches the common pipeline convention of a "_v002"-style suffix before the
extension, e.g. "Hero_modeling_v002.blend" -> 2. Used so a published
preview's revision number on Kitsu can be pinned to match the artist's own
file versioning, instead of relying only on Kitsu's auto-incrementing
counter.
"""

import os
import re

_VERSION_RE = re.compile(r"[._-]v(\d+)(?!\d)", re.IGNORECASE)


def parse_version_number(path):
    """Return the version number embedded in a filename, or None."""
    if not path:
        return None
    name = os.path.splitext(os.path.basename(path))[0]
    match = _VERSION_RE.search(name)
    if not match:
        return None
    return int(match.group(1))

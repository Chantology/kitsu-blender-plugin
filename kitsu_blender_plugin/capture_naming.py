"""Builds the review-file name for a full-scene video capture.

Convention (as requested by the studio using this plugin) for a shot:

    <Show>[_<Episode>]_<Sequence>_<Shot>_v<version>_<UserInitial+Lastname>

e.g. "MyShow_ep01_seq010_sh0100_v003_JDoe.mp4" (episodic) or
"MyShow_seq010_sh0100_v003_JDoe.mp4" (movie/short/commercial). For an asset
scene, which has no episode/sequence/shot, the analogous fallback
substitutes the asset type and asset name:

    <Show>_<AssetType>_<AssetName>_v<version>_<UserInitial+Lastname>

This is deliberately kept in its own module, separate from Kitsu publishing
itself and from the rest of the panel: review-file naming is one of the
most studio-specific parts of a pipeline, so a studio that wants a
different scheme only has to replace `build_capture_filename` here.
"""

import re

_ILLEGAL_CHARS_RE = re.compile(r'[\\/:*?"<>|]')


def _sanitize(token):
    """Make `token` safe as one filename segment: strip characters that are
    illegal in Windows/macOS/Linux file names and collapse internal
    whitespace (production/entity names are effectively single words in
    practice; this only guards against a stray space or slash breaking the
    resulting path)."""
    token = _ILLEGAL_CHARS_RE.sub("", str(token or ""))
    token = re.sub(r"\s+", "", token)
    return token


def user_initials(user):
    """First letter of the first name + full last name, e.g.
    "John Doe" -> "JDoe". Falls back to splitting `full_name` if a Kitsu
    account doesn't have first_name/last_name set as separate fields."""
    first_name = (user.get("first_name") or "").strip()
    last_name = (user.get("last_name") or "").strip()

    if not (first_name and last_name):
        parts = (user.get("full_name") or "").split()
        if len(parts) >= 2:
            first_name, last_name = parts[0], " ".join(parts[1:])
        elif parts:
            first_name, last_name = parts[0], ""

    return _sanitize(f"{first_name[:1]}{last_name}") or "unknown"


def build_capture_filename(show_name, breadcrumb, context, version, user):
    """Return a filename (without extension) for a full-scene capture.

    `breadcrumb` and `context` come straight from task_detection.detect_task
    — `breadcrumb`'s trailing element (the task type) is dropped here since
    it's not part of this naming convention. `version` is an int (from
    versioning.parse_version_number) or None, in which case that segment is
    simply omitted.
    """
    entity_names = list(breadcrumb[:-1])  # drop the trailing task type
    version_token = f"v{version:03d}" if version is not None else None
    user_token = user_initials(user)

    parts = [show_name] + entity_names + [version_token, user_token]
    return "_".join(_sanitize(part) for part in parts if part)

"""A per-session scratch directory for downloaded avatars/thumbnails/previews.

Deliberately a plain `tempfile` directory rather than a persisted addon-data
folder: everything written here (avatars, project thumbnails, comment
previews/attachments, drawover composites) is re-downloadable from Kitsu at
any time and tied to whichever account is currently logged in, so there is
nothing worth keeping around after Blender closes.
"""

import atexit
import shutil
import tempfile

_session_dir = None


def session_cache_dir():
    global _session_dir
    if _session_dir is None:
        _session_dir = tempfile.mkdtemp(prefix="kitsu_blender_plugin_")
        atexit.register(shutil.rmtree, _session_dir, ignore_errors=True)
    return _session_dir

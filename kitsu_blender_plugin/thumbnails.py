"""Icon-sized image previews (avatar, project thumbnail, attachment icons)
shown inline in a Panel, via Blender's `bpy.utils.previews`.

Confirmed against the Blender 5.2 API reference: this is the only
mechanism that draws an arbitrary raster image *inside* a plain Panel
widget (via `layout.operator(..., icon_value=...)` or similar) — it is
thumbnail/icon-sized, not a full embedded picture. See image_popup.py for
full-size viewing (revisions, drawovers, attachments), which needs an
actual Image Editor area instead.
"""

import bpy.utils.previews

_collection = None


def _get_collection():
    global _collection
    if _collection is None:
        _collection = bpy.utils.previews.new()
    return _collection


def load(key, file_path):
    """(Re)load `file_path` under `key`, returning its icon_id for use with
    `icon_value=`. Replaces any previous image already loaded under the
    same key (e.g. reloading the same project's thumbnail after a fresh
    login)."""
    collection = _get_collection()
    if key in collection:
        del collection[key]
    return collection.load(key, file_path, "IMAGE").icon_id


def icon_id(key):
    """The icon_id for `key`, or None if nothing has been loaded under it."""
    collection = _get_collection()
    entry = collection.get(key)
    return entry.icon_id if entry is not None else None


def clear():
    """Release the whole preview collection — call on addon unregister."""
    global _collection
    if _collection is not None:
        bpy.utils.previews.remove(_collection)
        _collection = None

"""Opens a downloaded image in a Blender Image Editor area.

Per the decided approach for this port: a Blender Panel has no widget that
embeds a full-size raster image the way Qt's QLabel+pixmap did in the
Maya/Houdini/Krita docks (see thumbnails.py for the icon-sized case that
*is* possible inline, via bpy.utils.previews) — so revisions, drawover
composites, and attachments are viewed by loading them into
`bpy.data.images` and pointing an Image Editor area at them: an existing
visible one is reused, or a new window is opened with one.
"""

import bpy


def _find_image_editor_area():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "IMAGE_EDITOR":
                return area
    return None


def _image_editor_space(area):
    for space in area.spaces:
        if space.type == "IMAGE_EDITOR":
            return space
    return None


def show_image(file_path, name=None):
    """Load `file_path` and show it in an Image Editor area, reusing one
    already visible if there is one, else opening a new window with one.
    Returns the loaded `bpy.data.images` entry."""
    image = bpy.data.images.load(file_path, check_existing=True)
    if name:
        image.name = name

    area = _find_image_editor_area()
    if area is None:
        bpy.ops.wm.window_new()
        area = bpy.context.window_manager.windows[-1].screen.areas[0]
        area.type = "IMAGE_EDITOR"

    space = _image_editor_space(area)
    if space is not None:
        space.image = image
    area.tag_redraw()
    return image

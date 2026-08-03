"""Kitsu (CGWire) production-tracking integration for Blender.

Blender counterpart of the Maya/Houdini/Krita Kitsu plugins: detects the
task a .blend file belongs to from its folder structure, shows
feedback/comments (including drawover attachments), and publishes new
versions with a comment and status change. See README.md for the full
feature list and installation instructions.
"""

bl_info = {
    "name": "Kitsu",
    "author": "Kitsu Blender Plugin contributors",
    "version": (1, 0, 2),
    "blender": (5, 2, 0),
    "location": "3D Viewport > Sidebar (N) > Kitsu",
    "description": (
        "Login, task detection, feedback/comments, and publish for a "
        "self-hosted Kitsu/Zou (CGWire) instance"
    ),
    "category": "Pipeline",
}

import bpy

from . import async_utils, operators, panels, preferences, properties, state, thumbnails

_CLASSES = (
    (preferences.KitsuAddonPreferences, properties.KitsuSessionProperties)
    + operators.CONNECTION_TASK_CLASSES
    + operators.FEEDBACK_CLASSES
    + operators.PUBLISH_CLASSES
    + panels.CONNECTION_TASK_CLASSES
    + panels.FEEDBACK_CLASSES
    + panels.PUBLISH_CLASSES
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    preferences.ensure_addon_entry()
    bpy.types.WindowManager.kitsu = bpy.props.PointerProperty(type=properties.KitsuSessionProperties)
    # Scene-scoped (not WindowManager, unlike the rest of this addon's UI
    # state): saved into the .blend file itself, so a manual task-type
    # override survives a Blender restart. See blender_scene.py's
    # load_task_type_override/save_task_type_override.
    bpy.types.Scene.kitsu_task_type_override_id = bpy.props.StringProperty(
        name="Kitsu Task Type Override",
        description=(
            "Manually-selected task type id for this file, remembered here "
            "when the folder structure's task-type name doesn't match any "
            "task type in Kitsu"
        ),
        default="",
    )

    # Deferred: at register() time (addon enable, or Blender startup) the
    # window/context isn't guaranteed fully ready yet — the same reason the
    # Maya port defers its own auto-login via QTimer.singleShot(0, ...).
    bpy.app.timers.register(operators.try_auto_login, first_interval=0.5)


def unregister():
    async_utils.unregister_periodic(operators._timesheet_auto_advance_tick)
    async_utils.unregister_periodic(operators._comment_poll_tick)
    if bpy.app.timers.is_registered(operators.try_auto_login):
        bpy.app.timers.unregister(operators.try_auto_login)

    del bpy.types.Scene.kitsu_task_type_override_id
    del bpy.types.WindowManager.kitsu

    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)

    thumbnails.clear()
    state.runtime.reset()


if __name__ == "__main__":
    register()

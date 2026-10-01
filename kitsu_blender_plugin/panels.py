"""bpy.types.Panel classes: the "Kitsu" tab in the 3D Viewport sidebar (N).

Direct replacement for the Maya/Houdini/Krita ports' Qt dock — Blender has
no bundled Qt, so this is a native rebuild with the same four sections
(Connection/Task/Feedback/Publish), not a line-for-line port of panel.py's
widget code. Each section is an ordinary Panel (`bl_order` controls
stacking); Blender's own click-to-collapse header behavior replaces
collapsible_section.py without any extra code.
"""

import os

import bpy

from . import state, thumbnails
from .preferences import get_preferences


class KITSU_PT_base:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Kitsu"


# template_icon's scale is in UI-icon units (~16px per unit at ui_scale
# 1.0), not pixels -- 5.0 approximates the other ports' 80px avatar/project
# thumbnail. row.label(icon_value=...) draws at a single fixed icon-sized
# slot, which is why both were much smaller than the other DCCs' ports.
_THUMBNAIL_SCALE = 5.0


def _draw_status(layout):
    if not state.runtime.status_message:
        return
    box = layout.box()
    box.alert = state.runtime.status_is_error
    icon = "ERROR" if state.runtime.status_is_error else "INFO"
    box.label(text=state.runtime.status_message, icon=icon)


def _status_name_for_task(task):
    status_id = task.get("task_status_id")
    for status in state.runtime.task_statuses:
        if status.get("id") == status_id:
            return status.get("name")
    return None


def _format_date(value):
    """Zou returns due_date as a full ISO datetime string
    ("2026-07-21T00:00:00") even though it's really just a date (Kitsu
    tasks don't have a time-of-day component) — show only the date part,
    matching the plain "YYYY-MM-DD" format used everywhere else in this
    addon (e.g. the timesheet's date parameter)."""
    if not value:
        return None
    return value.split("T", 1)[0]


class KITSU_PT_connection(KITSU_PT_base, bpy.types.Panel):
    bl_idname = "KITSU_PT_connection"
    bl_label = "Connection"
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        prefs = get_preferences(context)
        wm_props = context.window_manager.kitsu

        col = layout.column()
        col.prop(prefs, "server_url")
        col.prop(prefs, "email")
        col.prop(wm_props, "password")
        col.prop(prefs, "project_id")
        col.prop(prefs, "api_token")

        # Only once the server has said this account needs a 2FA code.
        if state.runtime.two_factor_methods:
            box = layout.box()
            box.label(text="Two-Factor Authentication", icon="LOCKED")
            if len(state.runtime.two_factor_methods) > 1:
                box.prop(wm_props, "two_factor_method")
            box.prop(wm_props, "two_factor_code")
            if wm_props.two_factor_method == "email_otp":
                box.operator("kitsu.send_email_code", icon="FILE_REFRESH")
            layout.operator("kitsu.login", text="Verify & Log In", icon="URL")
        else:
            layout.operator("kitsu.login", icon="URL")

        _draw_status(layout)

        if state.runtime.is_logged_in() and state.runtime.current_user:
            icon_id = thumbnails.icon_id("user_avatar")
            if icon_id:
                icon_row = layout.row()
                icon_row.alignment = "CENTER"
                icon_row.template_icon(icon_value=icon_id, scale=_THUMBNAIL_SCALE)
            user = state.runtime.current_user
            name = user.get("full_name") or user.get("email") or ""
            name_row = layout.row()
            name_row.alignment = "CENTER"
            name_row.label(text=f"Welcome, {name}")


class KITSU_PT_task(KITSU_PT_base, bpy.types.Panel):
    bl_idname = "KITSU_PT_task"
    bl_label = "Task"
    bl_order = 1

    @classmethod
    def poll(cls, context):
        return state.runtime.is_logged_in()

    def draw(self, context):
        layout = self.layout

        if state.runtime.project:
            icon_id = thumbnails.icon_id("project_thumbnail")
            if icon_id:
                icon_row = layout.row()
                icon_row.alignment = "CENTER"
                icon_row.template_icon(icon_value=icon_id, scale=_THUMBNAIL_SCALE)
            name_row = layout.row()
            name_row.alignment = "CENTER"
            name_row.label(text=state.runtime.project.get("name", ""))

        layout.operator("kitsu.detect_task", icon="FILE_REFRESH")

        # Shown here too (not just in Connection): a failed detection (e.g.
        # a folder structure that doesn't match Kitsu's expected layout)
        # needs to be visible right where the user just clicked, not only
        # in a different section they may not be looking at.
        _draw_status(layout)

        mismatch = state.runtime.task_type_mismatch
        if mismatch is not None:
            # The asset/shot itself resolved fine -- only its task-type
            # folder name didn't match anything in Kitsu (e.g. a
            # "modeling_blender" folder used to separate Blender files on
            # disk, still really a "Modeling" task). Offer a manual pick
            # instead of just reporting failure; this only ever appears in
            # that one specific case, so the normal picker-free layout
            # below is unaffected otherwise.
            layout.label(
                text=f"Folder task type '{mismatch.task_type_name}' didn't "
                "match any task type in this project — pick one:",
                icon="QUESTION",
            )
            wm_props = context.window_manager.kitsu
            layout.prop(wm_props, "task_type_override_choice", text="")
            layout.operator("kitsu.apply_task_type_override", icon="CHECKMARK")
            return

        task = state.runtime.task
        if task is None:
            layout.label(text="No task detected.")
            return

        layout.label(text=" / ".join(state.runtime.task_breadcrumb))

        status_name = _status_name_for_task(task)
        if status_name:
            layout.label(text=f"Status: {status_name}")
        due_date = _format_date(task.get("due_date"))
        layout.label(text=f"Due: {due_date or 'Not set'}")

        self._draw_timesheet(context, layout)

    def _draw_timesheet(self, context, layout):
        wm_props = context.window_manager.kitsu
        box = layout.box()
        # Defensive: this must never be anything but a plain number by the
        # time it reaches here (see kitsu_api.get_time_spent_today), but a
        # raised exception partway through this method would silently cut
        # off the rest of the panel below it (the slider, presets, Log
        # Time), so guard it anyway rather than trust every caller forever.
        # An explicit isinstance check, not `or 0` -- a non-empty list or
        # dict is truthy and would sail straight through `or 0` unchanged.
        logged_minutes = state.runtime.time_logged_today_minutes
        if not isinstance(logged_minutes, (int, float)):
            logged_minutes = 0
        box.label(text=f"Time logged today: {logged_minutes / 60:.1f}h")

        row = box.row()
        row.prop(wm_props, "timesheet_hours", slider=True, text="")

        suffix = " (auto)" if state.runtime.timesheet_auto_advance else ""
        box.label(text=f"Selected: {wm_props.timesheet_hours:.1f}h{suffix}")

        presets = box.row(align=True)
        for hours in (1.0, 4.0, 8.0):
            op = presets.operator("kitsu.set_timesheet_hours", text=f"{int(hours)}h")
            op.hours = hours

        box.operator("kitsu.log_time", icon="CHECKMARK")


CONNECTION_TASK_CLASSES = (
    KITSU_PT_connection,
    KITSU_PT_task,
)


_VIDEO_EXTENSIONS = {"mp4", "mov", "webm", "avi", "mkv"}


def _is_video_preview(preview):
    extension = (preview.get("extension") or "").lower().lstrip(".")
    return extension in _VIDEO_EXTENSIONS


def _format_comment_line(comment):
    prefix_bits = []
    previews = comment.get("previews") or []
    if previews and previews[0].get("revision"):
        prefix_bits.append(f"Revision {previews[0]['revision']}")
    status = comment.get("task_status") or {}
    status_name = status.get("short_name") or status.get("name")
    if status_name:
        prefix_bits.append(status_name.upper())

    person = comment.get("person") or {}
    person_name = person.get("full_name") or person.get("email") or "Someone"
    text = comment.get("text") or ""

    prefix = " - ".join(prefix_bits)
    return f"{prefix} - {person_name}: {text}" if prefix else f"{person_name}: {text}"


def sync_comment_items(context):
    """Rebuild wm.kitsu.comment_items from state.runtime.comments.

    Call this any time state.runtime.comments is reassigned (task
    (re)detection, the comment poll tick) so the template_list below stays
    in sync with the actual data.
    """
    kitsu = context.window_manager.kitsu
    kitsu.comment_items.clear()
    for comment in state.runtime.comments:
        item = kitsu.comment_items.add()
        item.label = _format_comment_line(comment)


class KITSU_UL_comments(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row()
        row.alert = index == state.runtime.selected_comment_index
        row.label(text=item.label)


class KITSU_PT_feedback(KITSU_PT_base, bpy.types.Panel):
    bl_idname = "KITSU_PT_feedback"
    bl_label = "Feedback"
    bl_order = 2

    @classmethod
    def poll(cls, context):
        return state.runtime.is_logged_in() and state.runtime.task is not None

    def draw(self, context):
        layout = self.layout
        # Shown here too, not just in Connection/Task: Feedback's own
        # operators (view/camera-background/checklist/save-attachments)
        # set the same shared status line, and it needs to be visible
        # right where the user is looking when one of them reports
        # something (e.g. the aspect-ratio mismatch warning).
        _draw_status(layout)

        comments = state.runtime.comments
        if not comments:
            layout.label(text="No comments yet.")
            return

        kitsu = context.window_manager.kitsu
        layout.template_list(
            "KITSU_UL_comments",
            "",
            kitsu,
            "comment_items",
            kitsu,
            "comment_active_index",
            rows=10,
        )

        selected = state.runtime.selected_comment_index
        if not (0 <= selected < len(comments)):
            return
        comment = comments[selected]

        checklist = comment.get("checklist") or []
        if checklist:
            box = layout.box()
            box.label(text="Checklist")
            for item_index, item in enumerate(checklist):
                op = box.operator(
                    "kitsu.toggle_checklist_item",
                    text=item.get("text") or "",
                    icon="CHECKBOX_HLT" if item.get("checked") else "CHECKBOX_DEHLT",
                    emboss=False,
                )
                op.comment_index = selected
                op.item_index = item_index

        previews = comment.get("previews") or []
        if previews:
            box = layout.box()
            box.label(text="Revisions")
            for preview_index, preview in enumerate(previews):
                self._draw_preview(box, selected, preview_index, preview)

        attachments = comment.get("attachment_files") or []
        if attachments:
            box = layout.box()
            box.label(text="Attachments")
            for attachment in attachments:
                row = box.row(align=True)
                icon_id = thumbnails.icon_id(f"attachment_{attachment.get('id')}")
                if icon_id:
                    row.label(text="", icon_value=icon_id)
                row.label(text=attachment.get("name") or attachment.get("extension") or "attachment")
            op = box.operator("kitsu.save_attachments", text="Save Attachment(s)", icon="EXPORT")
            op.comment_index = selected

    def _draw_preview(self, box, comment_index, preview_index, preview):
        row = box.row(align=True)
        icon_id = thumbnails.icon_id(f"preview_thumb_{preview.get('id')}")
        if icon_id:
            row.label(text="", icon_value=icon_id)
        label = f"Revision {preview.get('revision', '?')}"
        if preview.get("annotations"):
            label += " (with drawover)"
        row.label(text=label)

        buttons = box.row(align=True)
        if _is_video_preview(preview):
            op = buttons.operator("kitsu.add_timeline_markers", text="Add Timeline Markers")
            op.comment_index, op.preview_index = comment_index, preview_index
            op = buttons.operator("kitsu.show_video_drawovers", text="Show Drawovers in Viewport")
            op.comment_index, op.preview_index = comment_index, preview_index
        else:
            op = buttons.operator("kitsu.view_preview", text="View")
            op.comment_index, op.preview_index = comment_index, preview_index
            op = buttons.operator("kitsu.show_camera_background", text="Show as Camera Background")
            op.comment_index, op.preview_index = comment_index, preview_index


FEEDBACK_CLASSES = (KITSU_UL_comments, KITSU_PT_feedback)


class KITSU_PT_publish(KITSU_PT_base, bpy.types.Panel):
    bl_idname = "KITSU_PT_publish"
    bl_label = "Publish"
    bl_order = 3

    @classmethod
    def poll(cls, context):
        return state.runtime.is_logged_in() and state.runtime.task is not None

    def draw(self, context):
        layout = self.layout
        wm_props = context.window_manager.kitsu

        _draw_status(layout)

        layout.prop(wm_props, "status_id")
        layout.prop(wm_props, "comment")

        variations = state.runtime.publish_variations
        if variations:
            box = layout.box()
            box.label(text="Queued for publish (first = main preview):")
            for index, path in enumerate(variations):
                row = box.row(align=True)
                row.label(text=os.path.basename(path))
                op = row.operator("kitsu.remove_variation", text="", icon="X")
                op.index = index

        row = layout.row(align=True)
        row.operator("kitsu.add_variation_from_viewport", icon="IMAGE_DATA")
        row.operator("kitsu.add_variation_from_file", icon="FILE_IMAGE")

        layout.operator("kitsu.publish", icon="EXPORT")


class KITSU_PT_publish_capture(KITSU_PT_base, bpy.types.Panel):
    bl_idname = "KITSU_PT_publish_capture"
    bl_label = "Publish Capture"
    bl_parent_id = "KITSU_PT_publish"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return state.runtime.is_logged_in() and state.runtime.task is not None

    def draw(self, context):
        layout = self.layout
        layout.label(text="Export the full playback range as a video.")
        layout.operator("kitsu.publish_capture", icon="RENDER_ANIMATION")


PUBLISH_CLASSES = (
    KITSU_PT_publish,
    KITSU_PT_publish_capture,
)

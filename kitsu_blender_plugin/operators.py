"""bpy.types.Operator classes: every button the Kitsu panels expose.

Network calls always go through async_utils.run_async (never block
Blender's UI thread), and every callback that runs after the fact
(on_done/on_error, periodic timer ticks) reads `bpy.context` fresh rather
than closing over the `context` an operator's own execute()/invoke() was
given — Blender context references are only guaranteed valid for the
duration of that call, and closing over a stale one is exactly the kind of
bug the Krita port hit with Krita's own Window wrapper (see that port's
README/notes): always re-fetch, never cache across an async boundary.
"""

import datetime
import os
import shutil
import uuid

import bpy

from . import (
    annotations_raster,
    async_utils,
    blender_scene,
    capture_naming,
    image_popup,
    kitsu_api,
    paths,
    properties,
    state,
    task_detection,
    thumbnails,
    versioning,
    video_annotations,
)
from .preferences import get_preferences

_VIDEO_EXTENSIONS = {"mp4", "mov", "webm", "avi", "mkv"}


def _today_str():
    return datetime.date.today().isoformat()


def _set_status(message, is_error=False):
    state.runtime.status_message = message
    state.runtime.status_is_error = is_error


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class KITSU_OT_login(bpy.types.Operator):
    bl_idname = "kitsu.login"
    bl_label = "Login"
    bl_description = "Log in to Kitsu using the saved API token, or email + password"
    bl_options = {"REGISTER"}

    # Set only for the internal auto-login call so failures don't surface a
    # report the user never asked for — the same "silent unless it works"
    # behavior the Houdini/Krita ports use for auto-login.
    silent: bpy.props.BoolProperty(default=False, options={"HIDDEN"})

    def execute(self, context):
        prefs = get_preferences(context)

        server_url = prefs.server_url.strip()
        project_id = prefs.project_id.strip()
        api_token = prefs.api_token.strip()
        email = prefs.email.strip()
        # Read from the inline Connection-section field (WindowManager-
        # scoped — never serialized to the .blend or to Blender's saved
        # preferences, so this is as safe as the operator-popup-only
        # property it replaces, just visible without an extra click).
        password = context.window_manager.kitsu.password

        if not server_url:
            if not self.silent:
                self.report({"ERROR"}, "Server URL is empty.")
            return {"CANCELLED"}
        if not project_id:
            if not self.silent:
                self.report({"ERROR"}, "Project ID is empty.")
            return {"CANCELLED"}

        use_token = bool(api_token)
        if not use_token and not (email and password):
            if not self.silent:
                self.report({"ERROR"}, "Enter an email + password, or set an API token.")
            return {"CANCELLED"}

        _set_status("Logging in...")
        async_utils.tag_redraw_all()

        def work():
            session = kitsu_api.KitsuSession(server_url)
            if use_token:
                session.login_with_token(api_token)
            else:
                session.login(email, password)
            user = session.current_user()
            project = session.project(project_id)
            statuses = session.all_task_statuses(project)
            return session, user, project, statuses

        def on_done(result):
            session, user, project, statuses = result
            state.runtime.session = session
            state.runtime.current_user = user
            state.runtime.project = project
            state.runtime.task_statuses = statuses
            _set_status(f"Welcome, {user.get('full_name') or user.get('email') or ''}")

            new_refresh_token = session.get_refresh_token()
            if new_refresh_token:
                get_preferences(bpy.context).api_token = new_refresh_token

            _refresh_avatar_and_thumbnail(session, user, project)
            bpy.ops.kitsu.detect_task("EXEC_DEFAULT")
            async_utils.tag_redraw_all()

        def on_error(exc):
            state.runtime.session = None
            _set_status(str(exc), is_error=True)
            async_utils.tag_redraw_all()

        async_utils.run_async(work, on_done, on_error)
        return {"FINISHED"}


def _refresh_avatar_and_thumbnail(session, user, project):
    def work():
        cache_dir = paths.session_cache_dir()
        avatar_path = None
        thumb_path = None
        if user.get("id"):
            candidate = os.path.join(cache_dir, f"avatar_{user['id']}.png")
            try:
                session.download_person_avatar(user, candidate)
                avatar_path = candidate
            except Exception:
                avatar_path = None
        if project.get("id"):
            candidate = os.path.join(cache_dir, f"project_{project['id']}.png")
            try:
                session.download_project_thumbnail(project, candidate)
                thumb_path = candidate
            except Exception:
                thumb_path = None
        return avatar_path, thumb_path

    def on_done(result):
        avatar_path, thumb_path = result
        if avatar_path and os.path.isfile(avatar_path):
            thumbnails.load("user_avatar", avatar_path)
            state.runtime.user_avatar_path = avatar_path
        if thumb_path and os.path.isfile(thumb_path):
            thumbnails.load("project_thumbnail", thumb_path)
            state.runtime.project_thumbnail_path = thumb_path
        async_utils.tag_redraw_all()

    async_utils.run_async(work, on_done)


def try_auto_login():
    """Called once shortly after the addon registers (see __init__.py): if
    a server URL, project ID, and saved API token are all already filled
    in, log in without any user interaction, exactly like the Houdini/Krita
    ports' auto-login. A failed attempt (e.g. an expired token) fails
    silently into the status line rather than a popup — the user still has
    a normal Login button to fall back to."""
    prefs = get_preferences(bpy.context)
    if prefs.server_url.strip() and prefs.project_id.strip() and prefs.api_token.strip():
        bpy.ops.kitsu.login("EXEC_DEFAULT", silent=True)
    return None  # one-shot bpy.app.timers callback


# ---------------------------------------------------------------------------
# Task / timesheet
# ---------------------------------------------------------------------------


def _restart_timesheet_tracker(already_logged_minutes):
    # Defensive: gazu's actual per-person time-spent shape has surprised
    # this code twice already (see kitsu_api.get_time_spent_today) — an
    # unexpected type here would otherwise raise inside a bpy.app.timers
    # callback (on_done), silently aborting before the comment-poll timer
    # even gets registered further down the same on_done.
    if not isinstance(already_logged_minutes, (int, float)):
        already_logged_minutes = 0
    state.runtime.timesheet_base_minutes = already_logged_minutes
    state.runtime.timesheet_started_at = _now()
    state.runtime.timesheet_auto_advance = True
    properties.set_timesheet_hours_programmatically(min(12.0, already_logged_minutes / 60.0))


def _now():
    import time

    return time.time()


class KITSU_OT_detect_task(bpy.types.Operator):
    bl_idname = "kitsu.detect_task"
    bl_label = "Detect Task"
    bl_description = "Resolve the Kitsu task from this file's folder structure"

    def execute(self, context):
        if not state.runtime.is_logged_in():
            self.report({"ERROR"}, "Log in first.")
            return {"CANCELLED"}

        session = state.runtime.session
        project = state.runtime.project
        scene_path = blender_scene.scene_path()
        person_id = (state.runtime.current_user or {}).get("id")
        # Read now (main thread) -- work() below runs in a background
        # thread and must never touch bpy itself.
        remembered_task_type_id = blender_scene.load_task_type_override()

        def _finish(task, breadcrumb, ctx):
            minutes = session.get_time_spent_today(task, _today_str(), person_id)
            comments = session.all_comments_for_task(task)
            return ("resolved", task, breadcrumb, ctx, minutes, comments)

        def work():
            try:
                task, breadcrumb, ctx = task_detection.detect_task(session, project, scene_path)
            except task_detection.TaskTypeMismatch as exc:
                if remembered_task_type_id:
                    task_type = next(
                        (
                            t
                            for t in session.all_task_types_for_project(project)
                            if t.get("id") == remembered_task_type_id
                        ),
                        None,
                    )
                    if task_type:
                        task = session.get_task_by_entity(exc.entity, task_type)
                        if task:
                            breadcrumb = exc.breadcrumb + [task_type.get("name", "?")]
                            return _finish(task, breadcrumb, exc.context)
                # No remembered override (or it no longer resolves to a real
                # task) -- fetch the project's task types here, in the same
                # background call, so the panel can offer a picker without
                # another round-trip.
                task_types = session.all_task_types_for_project(project)
                return ("mismatch", exc, task_types)
            return _finish(task, breadcrumb, ctx)

        def on_done(payload):
            if payload[0] == "mismatch":
                _, exc, task_types = payload
                state.runtime.task = None
                state.runtime.task_breadcrumb = []
                state.runtime.task_context = None
                state.runtime.task_type_mismatch = exc
                state.runtime.task_type_options = task_types
                _set_status(str(exc), is_error=True)
                async_utils.tag_redraw_all()
                return

            _, task, breadcrumb, ctx, minutes, comments = payload
            state.runtime.task_type_mismatch = None
            state.runtime.task_type_options = []
            previous_task = state.runtime.task
            same_task = previous_task is not None and previous_task.get("id") == task.get("id")

            state.runtime.task = task
            state.runtime.task_breadcrumb = breadcrumb
            state.runtime.task_context = ctx
            state.runtime.comments = comments
            state.runtime.selected_comment_index = -1
            state.runtime.time_logged_today_minutes = minutes
            _set_status("")

            if not same_task:
                _restart_timesheet_tracker(minutes)
            # Re-detecting the same task leaves the tracker exactly as it
            # was (running or paused) — it must not reset just because the
            # user clicked Detect Task again to refresh comments.

            _ensure_timesheet_timer_registered()
            _ensure_comment_poll_registered()
            async_utils.tag_redraw_all()

        def on_error(exc):
            state.runtime.task = None
            state.runtime.task_breadcrumb = []
            state.runtime.task_context = None
            state.runtime.task_type_mismatch = None
            state.runtime.task_type_options = []
            _set_status(str(exc), is_error=True)
            async_utils.tag_redraw_all()

        async_utils.run_async(work, on_done, on_error)
        return {"FINISHED"}


class KITSU_OT_apply_task_type_override(bpy.types.Operator):
    bl_idname = "kitsu.apply_task_type_override"
    bl_label = "Use Selected Task Type"
    bl_description = (
        "Manually assign this file to the selected task type, and remember "
        "the choice for this file"
    )

    def execute(self, context):
        mismatch = state.runtime.task_type_mismatch
        if mismatch is None:
            return {"CANCELLED"}

        chosen_id = context.window_manager.kitsu.task_type_override_choice
        task_type = next(
            (t for t in state.runtime.task_type_options if t.get("id") == chosen_id),
            None,
        )
        if task_type is None:
            self.report({"ERROR"}, "Pick a task type first.")
            return {"CANCELLED"}

        session = state.runtime.session
        person_id = (state.runtime.current_user or {}).get("id")
        entity = mismatch.entity
        breadcrumb = mismatch.breadcrumb + [task_type.get("name", "?")]
        ctx = mismatch.context

        def work():
            task = session.get_task_by_entity(entity, task_type)
            if task is None:
                raise task_detection.TaskDetectionError(
                    f"{task_type.get('name', '?')!r} is a valid task type, "
                    "but this entity doesn't have a task of that type yet "
                    "in Kitsu."
                )
            minutes = session.get_time_spent_today(task, _today_str(), person_id)
            comments = session.all_comments_for_task(task)
            return task, minutes, comments

        def on_done(result):
            task, minutes, comments = result
            # Fresh bpy.context, not the execute()-scoped one above -- see
            # the module docstring on why callbacks never close over it.
            blender_scene.save_task_type_override(task_type.get("id"))

            state.runtime.task_type_mismatch = None
            state.runtime.task_type_options = []
            previous_task = state.runtime.task
            same_task = previous_task is not None and previous_task.get("id") == task.get("id")

            state.runtime.task = task
            state.runtime.task_breadcrumb = breadcrumb
            state.runtime.task_context = ctx
            state.runtime.comments = comments
            state.runtime.selected_comment_index = -1
            state.runtime.time_logged_today_minutes = minutes
            _set_status("")

            if not same_task:
                _restart_timesheet_tracker(minutes)

            _ensure_timesheet_timer_registered()
            _ensure_comment_poll_registered()
            async_utils.tag_redraw_all()

        def on_error(exc):
            _set_status(str(exc), is_error=True)
            async_utils.tag_redraw_all()

        async_utils.run_async(work, on_done, on_error)
        return {"FINISHED"}


class KITSU_OT_set_timesheet_hours(bpy.types.Operator):
    bl_idname = "kitsu.set_timesheet_hours"
    bl_label = "Set Hours"
    bl_description = "Set the timesheet slider to a preset value"

    hours: bpy.props.FloatProperty(default=0.0)

    def execute(self, context):
        # A real (non-suppressed) assignment: this is a manual override, so
        # it should pause auto-advance, same as dragging the slider by hand.
        context.window_manager.kitsu.timesheet_hours = self.hours
        return {"FINISHED"}


class KITSU_OT_log_time(bpy.types.Operator):
    bl_idname = "kitsu.log_time"
    bl_label = "Log Time"
    bl_description = "Set today's total logged time on this task to the slider value"

    def execute(self, context):
        if not state.runtime.is_logged_in() or state.runtime.task is None:
            self.report({"ERROR"}, "Detect a task first.")
            return {"CANCELLED"}

        session = state.runtime.session
        task = state.runtime.task
        person = state.runtime.current_user
        # Round to the nearest 0.5h: the slider drags continuously, but the
        # rest of the UI (and the Maya/Houdini/Krita ports) always work in
        # half-hour units.
        hours = round(context.window_manager.kitsu.timesheet_hours * 2) / 2
        minutes = int(round(hours * 60))

        def work():
            session.set_time_spent(task, person, _today_str(), minutes)
            return minutes

        def on_done(result):
            state.runtime.time_logged_today_minutes = result
            _restart_timesheet_tracker(result)
            _set_status(f"Logged {result / 60:.1f}h for today.")
            async_utils.tag_redraw_all()

        def on_error(exc):
            _set_status(str(exc), is_error=True)
            async_utils.tag_redraw_all()

        async_utils.run_async(work, on_done, on_error)
        return {"FINISHED"}


def _timesheet_auto_advance_tick():
    if not state.runtime.is_logged_in() or state.runtime.task is None:
        return True  # keep the timer alive but idle; cheap to just skip a tick
    if state.runtime.timesheet_auto_advance and state.runtime.timesheet_started_at is not None:
        elapsed_minutes = (_now() - state.runtime.timesheet_started_at) / 60.0
        total_minutes = state.runtime.timesheet_base_minutes + elapsed_minutes
        properties.set_timesheet_hours_programmatically(min(12.0, total_minutes / 60.0))
        async_utils.tag_redraw_all()
    return True


def _ensure_timesheet_timer_registered():
    async_utils.register_periodic(_timesheet_auto_advance_tick, 30.0)


# ---------------------------------------------------------------------------
# Comment auto-refresh (registered once a task is detected). The comment
# selection/checklist/attachment operators and the publish operators are
# appended further down in this same module.
# ---------------------------------------------------------------------------


def _comment_poll_tick():
    if not state.runtime.is_logged_in() or state.runtime.task is None:
        return True

    session = state.runtime.session
    task = state.runtime.task
    previous_ids = [c.get("id") for c in state.runtime.comments]
    selected_index = state.runtime.selected_comment_index
    selected_id = (
        previous_ids[selected_index]
        if 0 <= selected_index < len(previous_ids)
        else None
    )

    def work():
        return session.all_comments_for_task(task)

    def on_done(comments):
        new_ids = [c.get("id") for c in comments]
        if new_ids == previous_ids:
            return  # nothing changed — leave the list (and selection) alone
        state.runtime.comments = comments
        if selected_id is not None and selected_id in new_ids:
            state.runtime.selected_comment_index = new_ids.index(selected_id)
        else:
            state.runtime.selected_comment_index = -1
        async_utils.tag_redraw_all()

    def on_error(exc):
        pass  # silent — retried on the next tick, per the Houdini/Krita ports

    async_utils.run_async(work, on_done, on_error)
    return True


def _ensure_comment_poll_registered():
    async_utils.register_periodic(_comment_poll_tick, 60.0)


CONNECTION_TASK_CLASSES = (
    KITSU_OT_login,
    KITSU_OT_detect_task,
    KITSU_OT_apply_task_type_override,
    KITSU_OT_set_timesheet_hours,
    KITSU_OT_log_time,
)


# ---------------------------------------------------------------------------
# Feedback (comments, checklist, revisions, attachments)
# ---------------------------------------------------------------------------


def _resolve_comment(comment_index):
    comments = state.runtime.comments
    if not (0 <= comment_index < len(comments)):
        return None
    return comments[comment_index]


def _resolve_preview(comment_index, preview_index):
    comment = _resolve_comment(comment_index)
    if comment is None:
        return None, None
    previews = comment.get("previews") or []
    if not (0 <= preview_index < len(previews)):
        return comment, None
    return comment, previews[preview_index]


def _is_video_preview(preview):
    extension = (preview.get("extension") or "").lower().lstrip(".")
    return extension in _VIDEO_EXTENSIONS


class KITSU_OT_select_comment(bpy.types.Operator):
    bl_idname = "kitsu.select_comment"
    bl_label = "Select Comment"
    bl_description = "Show this comment's checklist, revisions, and attachments"

    index: bpy.props.IntProperty()

    def execute(self, context):
        if _resolve_comment(self.index) is None:
            return {"CANCELLED"}
        state.runtime.selected_comment_index = self.index
        _ensure_comment_downloads(state.runtime.comments[self.index])
        return {"FINISHED"}


def _ensure_comment_downloads(comment):
    """Download (once, cached by id) thumbnails for a comment's revisions
    and attachments so they can be shown as inline icons — the full-res
    preview file itself is only fetched on demand, when a "View"/"Show as
    Camera Background" button is actually clicked (see
    KITSU_OT_view_preview / KITSU_OT_show_camera_background)."""
    session = state.runtime.session
    if session is None:
        return
    cache_dir = paths.session_cache_dir()
    previews = comment.get("previews") or []
    attachments = comment.get("attachment_files") or []

    def work():
        results = {}
        for preview in previews:
            preview_id = preview.get("id")
            key = f"preview_thumb_{preview_id}"
            if not preview_id or key in state.runtime.download_cache:
                continue
            thumb_path = os.path.join(cache_dir, f"{key}.png")
            try:
                if _is_video_preview(preview):
                    session.download_preview_thumbnail(preview, thumb_path)
                else:
                    session.download_preview(preview, thumb_path)
            except Exception:
                continue
            results[key] = thumb_path

        for attachment in attachments:
            attachment_id = attachment.get("id")
            key = f"attachment_{attachment_id}"
            if not attachment_id or key in state.runtime.download_cache:
                continue
            extension = (attachment.get("extension") or "bin").lstrip(".")
            path = os.path.join(cache_dir, f"{key}.{extension}")
            try:
                session.download_attachment(attachment, path)
            except Exception:
                continue
            results[key] = path

        return results

    def on_done(results):
        for key, path in results.items():
            state.runtime.download_cache[key] = path
            thumbnails.load(key, path)
        async_utils.tag_redraw_all()

    async_utils.run_async(work, on_done)


class KITSU_OT_toggle_checklist_item(bpy.types.Operator):
    bl_idname = "kitsu.toggle_checklist_item"
    bl_label = "Toggle Checklist Item"
    bl_description = "Sync this checklist item's checked state to Kitsu"

    comment_index: bpy.props.IntProperty()
    item_index: bpy.props.IntProperty()

    def execute(self, context):
        comment = _resolve_comment(self.comment_index)
        if comment is None:
            return {"CANCELLED"}
        checklist = comment.get("checklist") or []
        if not (0 <= self.item_index < len(checklist)):
            return {"CANCELLED"}

        item = checklist[self.item_index]
        previous = bool(item.get("checked"))
        item["checked"] = not previous
        async_utils.tag_redraw_all()

        session = state.runtime.session
        comment_id = comment.get("id")

        def work():
            session.update_comment_checklist(comment_id, checklist)

        def on_done(_result):
            pass  # already applied optimistically above

        def on_error(exc):
            item["checked"] = previous  # roll back — the sync failed
            _set_status(f"Could not sync checklist: {exc}", is_error=True)
            async_utils.tag_redraw_all()

        async_utils.run_async(work, on_done, on_error)
        return {"FINISHED"}


def _download_full_preview(session, preview):
    """Blocking download of a preview's full-resolution file — called only
    from within an async work() function (a background thread), never
    directly from an operator's execute()."""
    cache_dir = paths.session_cache_dir()
    extension = (preview.get("extension") or "png").lstrip(".")
    path = os.path.join(cache_dir, f"preview_full_{preview['id']}.{extension}")
    if not os.path.isfile(path):
        session.download_preview(preview, path)
    return path


class KITSU_OT_view_preview(bpy.types.Operator):
    bl_idname = "kitsu.view_preview"
    bl_label = "View"
    bl_description = "Open this revision, with any drawover composited on top, in an Image Editor"

    comment_index: bpy.props.IntProperty()
    preview_index: bpy.props.IntProperty()

    def execute(self, context):
        comment, preview = _resolve_preview(self.comment_index, self.preview_index)
        if preview is None:
            self.report({"ERROR"}, "Preview not found.")
            return {"CANCELLED"}
        if _is_video_preview(preview):
            self.report({"ERROR"}, "This is a video revision — use the video buttons below instead.")
            return {"CANCELLED"}

        session = state.runtime.session
        revision = preview.get("revision")

        # Only the network download runs in the background thread — the
        # drawover composite touches bpy.data.images (to read/write pixels),
        # so it must run in on_done() on the main thread, never in work().
        def work():
            return _download_full_preview(session, preview)

        def on_done(raw_path):
            image_path = _composite_drawover_or_plain(raw_path, preview)
            image_popup.show_image(image_path, name=f"Kitsu_r{revision}")
            async_utils.tag_redraw_all()

        def on_error(exc):
            _set_status(f"Could not open preview: {exc}", is_error=True)
            async_utils.tag_redraw_all()

        async_utils.run_async(work, on_done, on_error)
        return {"FINISHED"}


def _composite_drawover_or_plain(raw_path, preview):
    """Composite `preview`'s drawover onto `raw_path` if it has one,
    returning the composited path — or `raw_path` unchanged otherwise (or
    if compositing fails, since a broken drawover shouldn't block viewing
    the plain image). Must run on the main thread (see callers)."""
    objects = annotations_raster.picture_annotation_objects(preview)
    if not objects:
        return raw_path
    composited_path = os.path.join(
        paths.session_cache_dir(), f"preview_{preview['id']}_drawover.png"
    )
    try:
        annotations_raster.render_annotations_to_file(raw_path, objects, composited_path)
        return composited_path
    except Exception as exc:
        _set_status(f"Could not render drawover, showing plain image: {exc}", is_error=True)
        return raw_path


class KITSU_OT_show_camera_background(bpy.types.Operator):
    bl_idname = "kitsu.show_camera_background"
    bl_label = "Show as Camera Background"
    bl_description = "Show this revision's drawover as the active camera's background image"

    comment_index: bpy.props.IntProperty()
    preview_index: bpy.props.IntProperty()

    def execute(self, context):
        comment, preview = _resolve_preview(self.comment_index, self.preview_index)
        if preview is None:
            self.report({"ERROR"}, "Preview not found.")
            return {"CANCELLED"}
        if _is_video_preview(preview):
            self.report({"ERROR"}, "This is a video revision — use the video buttons below instead.")
            return {"CANCELLED"}

        session = state.runtime.session
        revision = preview.get("revision")

        # As in KITSU_OT_view_preview: only the download runs in the
        # background thread, compositing happens in on_done() on the main
        # thread since it touches bpy.data.images.
        def work():
            return _download_full_preview(session, preview)

        def on_done(raw_path):
            image_path = _composite_drawover_or_plain(raw_path, preview)
            try:
                _bg, warning = blender_scene.set_feedback_camera_background(image_path, revision)
                if warning:
                    _set_status(warning, is_error=True)
            except blender_scene.BlenderSceneError as exc:
                _set_status(str(exc), is_error=True)
            async_utils.tag_redraw_all()

        def on_error(exc):
            _set_status(f"Could not show camera background: {exc}", is_error=True)
            async_utils.tag_redraw_all()

        async_utils.run_async(work, on_done, on_error)
        return {"FINISHED"}


class KITSU_OT_add_timeline_markers(bpy.types.Operator):
    bl_idname = "kitsu.add_timeline_markers"
    bl_label = "Add Timeline Markers"
    bl_description = "Add a timeline marker on every annotated frame of this video revision"

    comment_index: bpy.props.IntProperty()
    preview_index: bpy.props.IntProperty()

    def execute(self, context):
        comment, preview = _resolve_preview(self.comment_index, self.preview_index)
        if preview is None or not _is_video_preview(preview):
            self.report({"ERROR"}, "Video preview not found.")
            return {"CANCELLED"}

        fps = blender_scene.scene_fps()
        frames = video_annotations.annotated_frames(preview, fps)
        if not frames:
            self.report({"INFO"}, "This revision has no annotated frames.")
            return {"CANCELLED"}

        start = blender_scene.playback_start()
        revision = preview.get("revision")
        prefix = f"Kitsu r{revision} "
        frame_labels = {start + frame: f"{prefix}f{frame}" for frame in frames}

        blender_scene.set_timeline_markers(prefix, frame_labels)
        self.report({"INFO"}, f"Added {len(frame_labels)} timeline marker(s).")
        return {"FINISHED"}


class KITSU_OT_show_video_drawovers(bpy.types.Operator):
    bl_idname = "kitsu.show_video_drawovers"
    bl_label = "Show Drawovers in Viewport"
    bl_description = (
        "Show this video revision's per-frame drawovers as the active "
        "camera's background, matched to the exact frames they were drawn on"
    )

    comment_index: bpy.props.IntProperty()
    preview_index: bpy.props.IntProperty()

    def execute(self, context):
        comment, preview = _resolve_preview(self.comment_index, self.preview_index)
        if preview is None or not _is_video_preview(preview):
            self.report({"ERROR"}, "Video preview not found.")
            return {"CANCELLED"}

        fps = blender_scene.scene_fps()
        frames = video_annotations.annotated_frames(preview, fps)
        if not frames:
            self.report({"INFO"}, "This revision has no annotated frames.")
            return {"CANCELLED"}

        start = blender_scene.playback_start()
        revision = preview.get("revision")

        # Cover the FULL playback range, not just the span between the
        # first and last annotated frame: a MovieClip has no data at all
        # outside the numbered files it actually finds, and Blender shows
        # a "missing texture" magenta placeholder for any scene frame it's
        # asked to display outside that range. Writing a blank/transparent
        # file for every frame in the playback range (not only the
        # annotated ones) means there's always something valid loaded
        # while scrubbing before the first or after the last drawover.
        first_blender_frame, last_blender_frame = blender_scene.playback_range()
        frame_count = last_blender_frame - first_blender_frame + 1

        canvas_width, canvas_height = blender_scene.render_resolution()
        sequence_dir = os.path.join(paths.session_cache_dir(), f"drawover_seq_r{revision}")
        os.makedirs(sequence_dir, exist_ok=True)
        file_pattern = "frame.%04d.png"

        # Rendered synchronously (no network I/O involved — just local
        # compositing) rather than via run_async; a brief pause proportional
        # to the frame range is an acceptable cost for a discrete, explicit
        # button click, same as the equivalent Houdini/Maya actions.
        for blender_frame in range(first_blender_frame, last_blender_frame + 1):
            kitsu_frame = blender_frame - start
            out_path = os.path.join(sequence_dir, file_pattern % blender_frame)
            try:
                annotations_raster.render_video_frame_to_file(
                    frames.get(kitsu_frame, []), out_path, canvas_width, canvas_height
                )
            except Exception:
                continue

        try:
            blender_scene.set_feedback_drawover_sequence(
                sequence_dir, file_pattern, first_blender_frame, frame_count, revision
            )
        except blender_scene.BlenderSceneError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class KITSU_OT_save_attachments(bpy.types.Operator):
    bl_idname = "kitsu.save_attachments"
    bl_label = "Save Attachment(s)"
    bl_description = "Save this comment's attachments to a folder you choose"

    comment_index: bpy.props.IntProperty()
    directory: bpy.props.StringProperty(subtype="DIR_PATH")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        comment = _resolve_comment(self.comment_index)
        if comment is None:
            return {"CANCELLED"}

        saved = 0
        for attachment in comment.get("attachment_files") or []:
            cached_path = state.runtime.download_cache.get(f"attachment_{attachment.get('id')}")
            if not cached_path or not os.path.isfile(cached_path):
                continue
            dest = os.path.join(self.directory, os.path.basename(cached_path))
            try:
                shutil.copyfile(cached_path, dest)
                saved += 1
            except OSError:
                continue

        self.report({"INFO"}, f"Saved {saved} attachment(s) to {self.directory}")
        return {"FINISHED"}


FEEDBACK_CLASSES = (
    KITSU_OT_select_comment,
    KITSU_OT_toggle_checklist_item,
    KITSU_OT_view_preview,
    KITSU_OT_show_camera_background,
    KITSU_OT_add_timeline_markers,
    KITSU_OT_show_video_drawovers,
    KITSU_OT_save_attachments,
)


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def _resolve_status(status_id):
    return next((s for s in state.runtime.task_statuses if s.get("id") == status_id), None)


class KITSU_OT_publish(bpy.types.Operator):
    bl_idname = "kitsu.publish"
    bl_label = "Publish"
    bl_description = (
        "Post the status change and comment. If any image(s) are queued "
        "below, publishes them as a new revision (first = main preview, "
        "rest = variations); otherwise posts the status/comment alone, "
        "with no preview — the viewport is never captured automatically"
    )

    def execute(self, context):
        if not state.runtime.is_logged_in() or state.runtime.task is None:
            self.report({"ERROR"}, "Detect a task first.")
            return {"CANCELLED"}

        wm_props = context.window_manager.kitsu
        status = _resolve_status(wm_props.status_id)
        if status is None:
            self.report({"ERROR"}, "Choose a status first.")
            return {"CANCELLED"}
        comment_text = wm_props.comment

        session = state.runtime.session
        task = state.runtime.task
        # Snapshot the queue now: a variation added mid-upload (unlikely,
        # but the UI doesn't block) shouldn't be silently swept into this
        # publish or lost from view.
        variations = list(state.runtime.publish_variations)

        if not variations:
            # Nothing was explicitly queued to show — post the status
            # change/comment alone, with no preview. Never fall back to
            # capturing the viewport: what gets shown on a task is the
            # artist's call, made explicitly via "+ From Viewport" /
            # "+ From File...", not an automatic side effect of a status
            # change like switching to WIP just to signal starting work.
            def work():
                session.add_comment(task, status, comment=comment_text)

            def on_done(_result):
                _set_status("Published (status/comment only, no preview).")
                bpy.ops.kitsu.detect_task("EXEC_DEFAULT")  # refresh comments to show the new one
                async_utils.tag_redraw_all()

            def on_error(exc):
                _set_status(f"Publish failed: {exc}", is_error=True)
                async_utils.tag_redraw_all()

            async_utils.run_async(work, on_done, on_error)
            return {"FINISHED"}

        revision = versioning.parse_version_number(blender_scene.scene_path())
        main_path, *extra_paths = variations

        def work():
            comment, preview_file = session.publish_preview(
                task, status, comment=comment_text, preview_file_path=main_path, revision=revision
            )
            failed = []
            for variation_path in extra_paths:
                try:
                    session.add_extra_preview(task, comment, preview_file, variation_path)
                except Exception:
                    failed.append(variation_path)
            return failed

        def on_done(failed_variations):
            # Only the variations that failed stay queued — successful ones
            # are cleared, matching "the queue is cleared as each variation
            # uploads successfully" from the other ports. The main image is
            # only dropped from the queue once the whole publish (including
            # its own upload inside publish_preview) has actually succeeded.
            state.runtime.publish_variations = failed_variations
            if failed_variations:
                _set_status(
                    f"Published, but {len(failed_variations)} variation(s) "
                    "failed to upload — still queued for retry.",
                    is_error=True,
                )
            else:
                _set_status("Published.")
            bpy.ops.kitsu.detect_task("EXEC_DEFAULT")  # refresh comments to show the new one
            async_utils.tag_redraw_all()

        def on_error(exc):
            _set_status(f"Publish failed: {exc}", is_error=True)
            async_utils.tag_redraw_all()

        async_utils.run_async(work, on_done, on_error)
        return {"FINISHED"}


class KITSU_OT_add_variation_from_viewport(bpy.types.Operator):
    bl_idname = "kitsu.add_variation_from_viewport"
    bl_label = "+ From Viewport"
    bl_description = "Capture the current viewport and queue it as an image to publish"

    def execute(self, context):
        path = os.path.join(paths.session_cache_dir(), f"variation_{uuid.uuid4().hex}.png")
        try:
            blender_scene.capture_viewport(
                path, get_preferences(context).preview_render_time_limit
            )
        except blender_scene.BlenderSceneError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        state.runtime.publish_variations.append(path)
        return {"FINISHED"}


class KITSU_OT_add_variation_from_file(bpy.types.Operator):
    bl_idname = "kitsu.add_variation_from_file"
    bl_label = "+ From File..."
    bl_description = "Queue an existing image file as an image to publish"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.png;*.jpg;*.jpeg;*.bmp;*.tif;*.tiff", options={"HIDDEN"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        if not self.filepath or not os.path.isfile(self.filepath):
            self.report({"ERROR"}, "File not found.")
            return {"CANCELLED"}
        state.runtime.publish_variations.append(self.filepath)
        return {"FINISHED"}


class KITSU_OT_remove_variation(bpy.types.Operator):
    bl_idname = "kitsu.remove_variation"
    bl_label = "Remove"
    bl_description = "Remove this queued variation"

    index: bpy.props.IntProperty()

    def execute(self, context):
        variations = state.runtime.publish_variations
        if 0 <= self.index < len(variations):
            variations.pop(self.index)
        return {"FINISHED"}


class KITSU_OT_publish_capture(bpy.types.Operator):
    bl_idname = "kitsu.publish_capture"
    bl_label = "Publish Capture"
    bl_description = "Export the full playback range as a video and publish it as a new Kitsu revision"

    status_id: bpy.props.EnumProperty(name="Status", items=properties._status_items)
    comment: bpy.props.StringProperty(name="Comment", default="")

    def invoke(self, context, event):
        if not state.runtime.is_logged_in() or state.runtime.task is None:
            self.report({"ERROR"}, "Detect a task first.")
            return {"CANCELLED"}
        if not blender_scene.scene_path():
            self.report({"ERROR"}, "Save the scene first.")
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        status = _resolve_status(self.status_id)
        if status is None:
            self.report({"ERROR"}, "Choose a status first.")
            return {"CANCELLED"}

        session = state.runtime.session
        task = state.runtime.task
        breadcrumb = state.runtime.task_breadcrumb
        task_context = state.runtime.task_context
        project = state.runtime.project or {}
        user = state.runtime.current_user or {}
        scene_path = blender_scene.scene_path()
        revision = versioning.parse_version_number(scene_path)

        filename = capture_naming.build_capture_filename(
            project.get("name", ""), breadcrumb, task_context, revision, user
        )
        capture_dir = os.path.join(os.path.dirname(scene_path), "capture")
        os.makedirs(capture_dir, exist_ok=True)
        output_path = os.path.join(capture_dir, f"{filename}.mp4")

        try:
            blender_scene.capture_viewport_video(output_path)
        except blender_scene.BlenderSceneError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        comment_text = self.comment

        def work():
            session.publish_preview(
                task, status, comment=comment_text, preview_file_path=output_path, revision=revision
            )

        def on_done(_result):
            _set_status(f"Published capture: {output_path}")
            bpy.ops.kitsu.detect_task("EXEC_DEFAULT")
            async_utils.tag_redraw_all()

        def on_error(exc):
            _set_status(
                f"Capture saved to {output_path}, but publishing it failed: {exc}",
                is_error=True,
            )
            async_utils.tag_redraw_all()

        async_utils.run_async(work, on_done, on_error)
        return {"FINISHED"}


PUBLISH_CLASSES = (
    KITSU_OT_publish,
    KITSU_OT_add_variation_from_viewport,
    KITSU_OT_add_variation_from_file,
    KITSU_OT_remove_variation,
    KITSU_OT_publish_capture,
)

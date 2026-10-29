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
    panels,
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
# What "+ From File..." accepts as a revision's main preview (see
# KITSU_OT_add_variation_from_file), same as the Nuke port.
_PUBLISH_VIDEO_EXTENSIONS = (".mp4", ".mov")


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

        # Only sent once the server has asked for it (TwoFactorRequired below).
        wm_props = context.window_manager.kitsu
        two_factor_method = None
        two_factor_code = None
        if state.runtime.two_factor_methods and not use_token:
            two_factor_method = wm_props.two_factor_method
            two_factor_code = wm_props.two_factor_code.strip()
            if not two_factor_code:
                if not self.silent:
                    self.report({"ERROR"}, "Enter your two-factor code.")
                return {"CANCELLED"}

        _set_status("Logging in...")
        async_utils.tag_redraw_all()

        def work():
            session = kitsu_api.KitsuSession(server_url)
            if use_token:
                session.login_with_token(api_token)
            else:
                session.login(email, password, two_factor_method, two_factor_code)
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
            # Possibly a different project now -- the picker's task types
            # must be reloaded for it (the ShotTree is already keyed by
            # project id, see _cached_shot_tree).
            state.runtime.task_type_options = []
            state.runtime.two_factor_methods = []
            bpy.context.window_manager.kitsu.two_factor_code = ""
            _set_status(f"Welcome, {user.get('full_name') or user.get('email') or ''}")

            new_refresh_token = session.get_refresh_token()
            if new_refresh_token:
                get_preferences(bpy.context).api_token = new_refresh_token

            _refresh_avatar_and_thumbnail(session, user, project)
            bpy.ops.kitsu.detect_task("EXEC_DEFAULT")
            async_utils.tag_redraw_all()

        def on_error(exc):
            state.runtime.session = None
            wm_props = bpy.context.window_manager.kitsu
            if isinstance(exc, kitsu_api.TwoFactorRequired):
                # Password was right — reveal the code field and ask again.
                state.runtime.two_factor_methods = exc.methods
                wm_props.two_factor_method = exc.preferred
                wm_props.two_factor_code = ""
                _set_status(str(exc))
            elif isinstance(exc, kitsu_api.WrongTwoFactorCode):
                wm_props.two_factor_code = ""
                _set_status(str(exc), is_error=True)
            else:
                state.runtime.two_factor_methods = []
                _set_status(str(exc), is_error=True)
            async_utils.tag_redraw_all()

        async_utils.run_async(work, on_done, on_error)
        return {"FINISHED"}


class KITSU_OT_send_email_code(bpy.types.Operator):
    bl_idname = "kitsu.send_email_code"
    bl_label = "Send Code"
    bl_description = "Email a new two-factor code to this account"
    bl_options = {"REGISTER"}

    def execute(self, context):
        prefs = get_preferences(context)
        server_url = prefs.server_url.strip()
        email = prefs.email.strip()
        if not (server_url and email):
            self.report({"ERROR"}, "Enter the server URL and email first.")
            return {"CANCELLED"}

        _set_status("Sending code...")
        async_utils.tag_redraw_all()

        def work():
            kitsu_api.KitsuSession(server_url).send_email_code(email)

        def on_done(_result):
            _set_status(kitsu_api.two_factor_prompt("email_otp"))
            async_utils.tag_redraw_all()

        def on_error(exc):
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


def _apply_resolved_task(task, breadcrumb, ctx, minutes, comments, manual=False):
    """Finish populating the Task section for a resolved task, whether it
    came straight out of folder detection or via the task picker (a
    remembered choice, or one just made by hand). Main thread only (an
    on_done callback)."""
    state.runtime.task_picker_visible = False
    previous_task = state.runtime.task
    same_task = previous_task is not None and previous_task.get("id") == task.get("id")

    state.runtime.task = task
    state.runtime.task_breadcrumb = breadcrumb
    state.runtime.task_context = ctx
    state.runtime.task_manual = manual
    state.runtime.comments = comments
    state.runtime.selected_comment_index = -1
    panels.sync_comment_items(bpy.context)
    bpy.context.window_manager.kitsu.comment_active_index = -1
    state.runtime.time_logged_today_minutes = minutes
    _set_status("")

    if not same_task:
        _restart_timesheet_tracker(minutes)
    # Re-detecting the same task leaves the tracker exactly as it was
    # (running or paused) — it must not reset just because the user
    # clicked Detect Task again to refresh comments.

    _ensure_timesheet_timer_registered()
    _ensure_comment_poll_registered()
    async_utils.tag_redraw_all()


def _clear_task():
    state.runtime.task = None
    state.runtime.task_breadcrumb = []
    state.runtime.task_context = None
    state.runtime.task_manual = False


# ---- task picker (manual episode / sequence / shot / task type) ----


def _store_shot_tree(tree):
    state.runtime.shot_tree = tree
    state.runtime.shot_tree_project_id = (state.runtime.project or {}).get("id")


def _cached_shot_tree():
    """The loaded ShotTree, or None if there isn't one for the current
    project yet (e.g. after switching projects)."""
    project_id = (state.runtime.project or {}).get("id")
    if project_id and state.runtime.shot_tree_project_id == project_id:
        return state.runtime.shot_tree
    return None


def _set_picker_enum(wm_props, name, value):
    """Select `value` in one of the picker's dynamic-enum dropdowns, or its
    "nothing picked" entry if `value` is None or (no longer) one of its
    items — assigning an identifier Blender doesn't know raises."""
    try:
        setattr(wm_props, name, value or properties.PICKER_NONE)
    except (TypeError, ValueError):
        setattr(wm_props, name, properties.PICKER_NONE)


def _prefill_task_picker():
    """Preselect the current shot / task type in the picker's dropdowns.
    Top-down, since each dropdown's update callback empties the ones
    below it (see properties._on_picker_episode_update)."""
    wm_props = bpy.context.window_manager.kitsu
    tree = _cached_shot_tree()
    shot = None
    episode = sequence = None
    if state.runtime.picker_context == "shot" and state.runtime.picker_entity and tree:
        entity = state.runtime.picker_entity
        # The tree's own copy carries parent_id, whatever lookup the
        # entity itself came from.
        shot = tree.find_shot(entity.get("id")) or entity
        episode, sequence = tree.parents(shot)

    _set_picker_enum(wm_props, "picker_episode", episode and episode.get("id"))
    _set_picker_enum(wm_props, "picker_sequence", sequence and sequence.get("id"))
    _set_picker_enum(wm_props, "picker_shot", shot and shot.get("id"))

    task = state.runtime.task
    task_type_id = task.get("task_type_id") if task else None
    if not any(t.get("id") == task_type_id for t in state.runtime.task_type_options):
        task_type_id = None
    _set_picker_enum(wm_props, "picker_task_type", task_type_id)

    state.runtime.picker_prefill_shot_id = shot.get("id") if shot else None
    state.runtime.picker_prefill_task_type_id = task_type_id


def _open_task_picker(hint):
    """Show the task picker, first loading (in the background) whatever it
    still needs: the project's ShotTree for a shot-context file, and the
    project's task types."""
    session = state.runtime.session
    project = state.runtime.project
    need_tree = state.runtime.picker_context == "shot" and _cached_shot_tree() is None
    need_task_types = not state.runtime.task_type_options

    if not (need_tree or need_task_types):
        _prefill_task_picker()
        state.runtime.task_picker_hint = hint
        state.runtime.task_picker_visible = True
        async_utils.tag_redraw_all()
        return

    state.runtime.task_picker_hint = "Loading shots…" if need_tree else "Loading task types…"
    state.runtime.task_picker_visible = True
    async_utils.tag_redraw_all()

    def work():
        tree = task_detection.ShotTree.load(session, project) if need_tree else None
        task_types = session.all_task_types_for_project(project) if need_task_types else None
        return tree, task_types

    def on_done(result):
        tree, task_types = result
        if tree is not None:
            _store_shot_tree(tree)
        if task_types is not None:
            state.runtime.task_type_options = task_types or []
        if not state.runtime.task_picker_visible:
            return  # hidden again (Change Task clicked twice) while loading
        _open_task_picker(hint)

    def on_error(exc):
        state.runtime.task_picker_visible = False
        _set_status(f"Could not load the project's shots: {exc}", is_error=True)
        async_utils.tag_redraw_all()

    async_utils.run_async(work, on_done, on_error)


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
        # thread and must never touch bpy itself. Remembered manual picks
        # win over the folders (see task_detection.detect_with_overrides).
        remembered_shot_id = blender_scene.load_shot_override()
        remembered_task_type_id = blender_scene.load_task_type_override()
        state.runtime.task_picker_visible = False

        def work():
            outcome = task_detection.detect_with_overrides(
                session, project, scene_path, remembered_shot_id, remembered_task_type_id
            )
            if outcome["result"] is None:
                # Unresolved -- fetch the project's task types here, in the
                # same background call, so the panel can offer the picker
                # without another round-trip.
                return outcome, session.all_task_types_for_project(project), None, None
            task = outcome["result"][0]
            minutes = session.get_time_spent_today(task, _today_str(), person_id)
            comments = session.all_comments_for_task(task)
            return outcome, None, minutes, comments

        def on_done(payload):
            outcome, task_types, minutes, comments = payload
            state.runtime.picker_entity = outcome["entity"]
            state.runtime.picker_breadcrumb = outcome["breadcrumb"]
            state.runtime.picker_context = outcome["context"]
            state.runtime.folder_task_type_name = outcome["task_type_name"]
            if outcome["shot_tree"] is not None:
                _store_shot_tree(outcome["shot_tree"])

            if outcome["result"] is not None:
                task, breadcrumb, ctx = outcome["result"]
                _apply_resolved_task(task, breadcrumb, ctx, minutes, comments, manual=outcome["manual"])
                return

            state.runtime.task_type_options = task_types or []
            _clear_task()
            _set_status(outcome["error"] or "No task detected.", is_error=True)
            if outcome["entity"] is None:
                hint = "Pick the shot this file belongs to:"
            else:
                hint = "Pick the task type:"
            _open_task_picker(hint)

        def on_error(exc):
            # TaskDetectionError (e.g. an asset that can't be found, or an
            # unsaved file) -- nothing for the picker to apply to.
            _clear_task()
            state.runtime.picker_entity = None
            state.runtime.picker_breadcrumb = None
            state.runtime.picker_context = None
            state.runtime.task_picker_visible = False
            _set_status(str(exc), is_error=True)
            async_utils.tag_redraw_all()

        async_utils.run_async(work, on_done, on_error)
        return {"FINISHED"}


class KITSU_OT_change_task(bpy.types.Operator):
    bl_idname = "kitsu.change_task"
    bl_label = "Change Task..."
    bl_description = (
        "Pick the shot and/or task type by hand. The choice is remembered "
        "for this file and wins over folder detection until reset"
    )

    def execute(self, context):
        if state.runtime.task_picker_visible:
            state.runtime.task_picker_visible = False
            async_utils.tag_redraw_all()
            return {"FINISHED"}
        if not state.runtime.is_logged_in():
            self.report({"ERROR"}, "Log in first.")
            return {"CANCELLED"}
        if state.runtime.picker_context is None:
            self.report(
                {"INFO"},
                "Click Detect Task first. The task can only be changed by hand "
                "for a file in a shot folder, or an asset that was found.",
            )
            return {"CANCELLED"}
        _open_task_picker("Pick a different shot and/or task type:")
        return {"FINISHED"}


class KITSU_OT_apply_task_pick(bpy.types.Operator):
    bl_idname = "kitsu.apply_task_pick"
    bl_label = "Use Selected"
    bl_description = (
        "Assign this file to the selected shot / task type, and remember "
        "the choice for this file"
    )

    def execute(self, context):
        if not state.runtime.is_logged_in():
            self.report({"ERROR"}, "Log in first.")
            return {"CANCELLED"}
        if state.runtime.picker_context is None:
            return {"CANCELLED"}

        wm_props = context.window_manager.kitsu
        entity = state.runtime.picker_entity
        breadcrumb = state.runtime.picker_breadcrumb
        ctx = state.runtime.picker_context

        shot_changed = False
        if ctx == "shot":
            tree = _cached_shot_tree()
            shot = tree.find_shot(wm_props.picker_shot) if tree else None
            if shot is None:
                self.report({"ERROR"}, "Pick a shot first.")
                return {"CANCELLED"}
            if shot.get("id") != state.runtime.picker_prefill_shot_id:
                shot_changed = True
                entity = shot
                breadcrumb = tree.breadcrumb(shot)
                state.runtime.picker_entity = entity
                state.runtime.picker_breadcrumb = breadcrumb
                state.runtime.picker_prefill_shot_id = shot.get("id")
                # Remembered right away, like the Nuke port's shot combo --
                # even if that shot turns out to have no task of the
                # folder's type, it's still the right shot.
                blender_scene.save_shot_override(shot.get("id"))
        if entity is None:
            self.report({"ERROR"}, "Pick a shot first.")
            return {"CANCELLED"}

        picked_id = wm_props.picker_task_type
        task_type = next(
            (t for t in state.runtime.task_type_options if t.get("id") == picked_id),
            None,
        )
        task_type_changed = (
            task_type is not None
            and task_type.get("id") != state.runtime.picker_prefill_task_type_id
        )
        if not (shot_changed or task_type_changed):
            if state.runtime.task is not None:
                state.runtime.task_picker_visible = False  # nothing changed
                return {"FINISHED"}
            if task_type is None:
                self.report({"ERROR"}, "Pick a task type first.")
                return {"CANCELLED"}

        session = state.runtime.session
        project = state.runtime.project
        person_id = (state.runtime.current_user or {}).get("id")
        remembered_task_type_id = blender_scene.load_task_type_override()
        folder_task_type_name = state.runtime.folder_task_type_name

        def work():
            # Task type: the one picked in the dropdown, else the
            # remembered pick, else the folder name.
            result = None
            if task_type is not None:
                result = task_detection.resolve_task_by_type_id(
                    session, project, entity, breadcrumb, ctx, task_type.get("id")
                )
                if result is None and task_type_changed:
                    return ("no_task_of_type", task_type.get("name", "?"))
            if result is None and remembered_task_type_id:
                result = task_detection.resolve_task_by_type_id(
                    session, project, entity, breadcrumb, ctx, remembered_task_type_id
                )
            if result is None:
                try:
                    result = task_detection.resolve_task(
                        session, project, entity, breadcrumb, folder_task_type_name, ctx
                    )
                except task_detection.TaskTypeMismatch:
                    return ("no_task", None)
            task = result[0]
            minutes = session.get_time_spent_today(task, _today_str(), person_id)
            comments = session.all_comments_for_task(task)
            return ("resolved", result, minutes, comments)

        def on_done(payload):
            kind = payload[0]
            if kind == "no_task_of_type":
                _set_status(
                    f"'{payload[1]}' is a valid task type, but this entity "
                    "doesn't have a task of that type yet in Kitsu.",
                    is_error=True,
                )
                async_utils.tag_redraw_all()
                return
            if kind == "no_task":
                _clear_task()
                _set_status(
                    " / ".join(breadcrumb) + " — no matching task, pick the task type:",
                    is_error=True,
                )
                state.runtime.task_picker_hint = "Pick the task type:"
                async_utils.tag_redraw_all()
                return

            _, (task, task_breadcrumb, task_ctx), minutes, comments = payload
            # blender_scene uses a fresh bpy.context, not the
            # execute()-scoped one above -- see the module docstring on
            # why callbacks never close over it.
            if task_type_changed:
                blender_scene.save_task_type_override(task_type.get("id"))
            _apply_resolved_task(task, task_breadcrumb, task_ctx, minutes, comments, manual=True)

        def on_error(exc):
            _set_status(f"Unexpected error resolving task: {exc}", is_error=True)
            async_utils.tag_redraw_all()

        async_utils.run_async(work, on_done, on_error)
        return {"FINISHED"}


class KITSU_OT_reset_task_overrides(bpy.types.Operator):
    bl_idname = "kitsu.reset_task_overrides"
    bl_label = "Reset to Folder Detection"
    bl_description = (
        "Forget the shot/task type picked by hand for this file and detect "
        "it from its folders again"
    )

    def execute(self, context):
        blender_scene.clear_overrides()
        return bpy.ops.kitsu.detect_task("EXEC_DEFAULT")


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
        panels.sync_comment_items(bpy.context)
        bpy.context.window_manager.kitsu.comment_active_index = state.runtime.selected_comment_index
        async_utils.tag_redraw_all()

    def on_error(exc):
        pass  # silent — retried on the next tick, per the Houdini/Krita ports

    async_utils.run_async(work, on_done, on_error)
    return True


def _ensure_comment_poll_registered():
    async_utils.register_periodic(_comment_poll_tick, 60.0)


CONNECTION_TASK_CLASSES = (
    KITSU_OT_login,
    KITSU_OT_send_email_code,
    KITSU_OT_detect_task,
    KITSU_OT_change_task,
    KITSU_OT_apply_task_pick,
    KITSU_OT_reset_task_overrides,
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
        "Post the status change and comment. If any image(s)/video(s) are "
        "queued below, publishes them as a new revision (first = main "
        "preview, rest = variations); otherwise posts the status/comment alone, "
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
    bl_description = (
        "Queue an existing image or video (e.g. an MP4 rendered on the "
        "farm) to publish as a revision"
    )

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(
        default="*.png;*.jpg;*.jpeg;*.bmp;*.tif;*.tiff;*.mp4;*.mov", options={"HIDDEN"}
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        if not self.filepath or not os.path.isfile(self.filepath):
            self.report({"ERROR"}, "File not found.")
            return {"CANCELLED"}
        if self.filepath.lower().endswith(_PUBLISH_VIDEO_EXTENSIONS):
            # A video (typically the farm-rendered MP4) is the deliverable,
            # so it becomes the main preview of the new revision rather
            # than a variation hanging off a viewport snapshot.
            state.runtime.publish_variations.insert(0, self.filepath)
        else:
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

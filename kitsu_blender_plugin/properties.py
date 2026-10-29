"""The handful of values Blender widgets must bind to via `layout.prop()`.

Everything else (session, task, comments, ...) lives in state.py's plain
Python singleton, since it holds gazu dicts and a live session object that
bpy's property system can't represent. This PropertyGroup only holds the
few user-editable fields an actual widget needs a real bpy property for:
the publish comment text, the chosen status, the timesheet hours slider,
the task picker's dropdowns, and the login password. It is registered on `WindowManager` (`wm.kitsu`)
rather than `Scene`, since none of this is scene/file data — it should not
be saved into a .blend and should behave the same regardless of which
file is open.

The password specifically lives here (not in `AddonPreferences`, which
Blender persists to disk in the user's preferences file) precisely
*because* `WindowManager` properties are never serialized anywhere — not
into the .blend file, not into Blender's saved preferences. It resets to
empty every time Blender restarts, the same "never written to disk"
guarantee the other three ports give via never putting the password in
`QSettings`.
"""

import bpy

# Blender's enum-items callback must keep a reference to the list it
# returns alive beyond the call (a well-known pitfall: the C side reads the
# strings back out of the Python list it was handed, and a list that gets
# garbage-collected immediately after return can crash Blender) — caching
# the last result in a module-level variable is the documented workaround.
_status_items_cache = []

# Same GC-safety concern as _status_items_cache, for the task picker's four
# dropdowns (episode/sequence/shot/task type), keyed by dropdown -- each is
# populated independently, and the sequence/shot ones change with the
# dropdown above them.
_picker_items_cache = {}

# Identifier of every picker dropdown's "nothing picked" first entry. It's
# always present, so resetting a dropdown to it is always valid whatever
# the dropdown above it currently holds.
PICKER_NONE = "NONE"

# Set while a periodic/programmatic update (the auto-advance timer, or
# restarting the tracker for a newly-detected task) is writing
# timesheet_hours, so _on_timesheet_hours_update can tell that apart from
# the user actually dragging the slider or clicking a preset — the same
# problem the Krita port solved with QSlider.blockSignals(), just without
# a signal-blocking API to reuse here.
_suppress_timesheet_update = False


def _status_items(self, context):
    from . import state

    global _status_items_cache
    items = [
        (status["id"], status.get("short_name") or status["name"], status["name"])
        for status in state.runtime.task_statuses
    ]
    if not items:
        items = [("NONE", "No statuses", "No task statuses loaded yet")]
    _status_items_cache = items
    return _status_items_cache


def _picker_items(key, placeholder, entities):
    items = [(PICKER_NONE, placeholder, placeholder)]
    for entity in entities:
        name = entity.get("name") or "?"
        items.append((entity["id"], name, name))
    # Hand back the very same cached list when nothing changed, so the
    # strings Blender may still be holding from the previous call stay
    # referenced (reading one dropdown's value from another's callback, as
    # the sequence/shot ones below do, re-runs that dropdown's callback).
    if _picker_items_cache.get(key) == items:
        return _picker_items_cache[key]
    _picker_items_cache[key] = items
    return items


def _picker_episode_items(self, context):
    from . import state

    tree = state.runtime.shot_tree
    return _picker_items("episode", "Select episode…", tree.episodes() if tree else [])


def _picker_sequence_items(self, context):
    from . import state

    tree = state.runtime.shot_tree
    if tree is None:
        sequences = []
    elif tree.episodic:
        episode_id = self.picker_episode
        sequences = tree.sequences(episode_id) if episode_id not in ("", PICKER_NONE) else []
    else:
        sequences = tree.sequences()
    return _picker_items("sequence", "Select sequence…", sequences)


def _picker_shot_items(self, context):
    from . import state

    tree = state.runtime.shot_tree
    sequence_id = self.picker_sequence
    shots = tree.shots(sequence_id) if tree and sequence_id not in ("", PICKER_NONE) else []
    return _picker_items("shot", "Select shot…", shots)


def _picker_task_type_items(self, context):
    from . import state

    return _picker_items("task_type", "Select task type…", state.runtime.task_type_options)


# Cascade: picking a different episode empties the sequence and shot
# dropdowns below it, a different sequence the shot one -- the same as the
# Nuke port's combos. Nothing is applied until "Use Selected" (see
# operators.KITSU_OT_apply_task_pick), consistent with the explicit button
# the old task-type-only picker had, and immune to enum updates firing
# while the picker is being prefilled.
def _on_picker_episode_update(self, context):
    self.picker_sequence = PICKER_NONE


def _on_picker_sequence_update(self, context):
    self.picker_shot = PICKER_NONE


def _on_timesheet_hours_update(self, context):
    if _suppress_timesheet_update:
        return
    from . import state

    state.runtime.timesheet_auto_advance = False


def _on_comment_active_index_update(self, context):
    from . import operators, state

    comment = operators._resolve_comment(self.comment_active_index)
    if comment is None:
        state.runtime.selected_comment_index = -1
        return
    state.runtime.selected_comment_index = self.comment_active_index
    operators._ensure_comment_downloads(comment)


def set_timesheet_hours_programmatically(value):
    """Set wm.kitsu.timesheet_hours without treating it as a manual
    override (i.e. without pausing auto-advance) — used by the auto-advance
    timer tick itself and by resetting the tracker for a newly-detected
    task."""
    global _suppress_timesheet_update
    _suppress_timesheet_update = True
    try:
        bpy.context.window_manager.kitsu.timesheet_hours = value
    finally:
        _suppress_timesheet_update = False


class KitsuCommentItem(bpy.types.PropertyGroup):
    """One row's display text for the Feedback panel's comment list.

    Mirrors state.runtime.comments (a plain Python list of gazu dicts)
    into a real bpy collection, since layout.template_list() -- the only
    way to get a scrollbar on a list in Blender's UI toolkit -- needs an
    actual CollectionProperty to bind to, not a Python list.
    """

    label: bpy.props.StringProperty()


_TWO_FACTOR_LABELS = {
    "totp": ("Authenticator App", "Code from your authenticator app"),
    "email_otp": ("Email Code", "Code emailed to you by Kitsu"),
    "recovery_code": ("Recovery Code", "One of your Kitsu recovery codes"),
}


# Same GC-safety concern as _status_items_cache.
_two_factor_method_items_cache = []


def _two_factor_method_items(self, context):
    from . import state

    global _two_factor_method_items_cache
    items = [
        (method, *_TWO_FACTOR_LABELS.get(method, (method, method)))
        for method in state.runtime.two_factor_methods
    ]
    if not items:
        items = [("NONE", "None", "")]
    _two_factor_method_items_cache = items
    return items


def _on_two_factor_method_changed(self, context):
    from . import state

    # Switching to email doesn't send anything by itself — the panel shows
    # a Send Code button for that, so a stray click can't spam the inbox.
    if self.two_factor_method == "email_otp":
        state.runtime.status_message = "Click Send Code, then enter the emailed code and log in again."
    else:
        from . import kitsu_api

        state.runtime.status_message = kitsu_api.two_factor_prompt(self.two_factor_method)
    state.runtime.status_is_error = False


class KitsuSessionProperties(bpy.types.PropertyGroup):
    password: bpy.props.StringProperty(
        name="Password",
        description="Kitsu account password (never saved to disk — cleared on every Blender restart)",
        subtype="PASSWORD",
        default="",
    )
    two_factor_method: bpy.props.EnumProperty(
        name="Method",
        description="Which two-factor method to enter a code for",
        items=_two_factor_method_items,
        update=_on_two_factor_method_changed,
    )
    two_factor_code: bpy.props.StringProperty(
        name="Code",
        description="Two-factor authentication code (only asked for when your account has 2FA enabled)",
        default="",
    )
    comment: bpy.props.StringProperty(
        name="Comment",
        description="Comment to publish alongside this revision",
        default="",
    )
    status_id: bpy.props.EnumProperty(
        name="Status",
        description="Task status to set when publishing",
        items=_status_items,
    )
    picker_episode: bpy.props.EnumProperty(
        name="Episode",
        description="Episode of the shot this file belongs to",
        items=_picker_episode_items,
        update=_on_picker_episode_update,
    )
    picker_sequence: bpy.props.EnumProperty(
        name="Sequence",
        description="Sequence of the shot this file belongs to",
        items=_picker_sequence_items,
        update=_on_picker_sequence_update,
    )
    picker_shot: bpy.props.EnumProperty(
        name="Shot",
        description="Shot this file belongs to",
        items=_picker_shot_items,
    )
    picker_task_type: bpy.props.EnumProperty(
        name="Task Type",
        description="Task type this file belongs to",
        items=_picker_task_type_items,
    )
    comment_items: bpy.props.CollectionProperty(type=KitsuCommentItem)
    comment_active_index: bpy.props.IntProperty(
        name="Selected Comment",
        default=-1,
        update=_on_comment_active_index_update,
    )
    timesheet_hours: bpy.props.FloatProperty(
        name="Hours",
        description="Hours worked today on this task",
        default=0.0,
        min=0.0,
        max=12.0,
        step=50,  # Blender FloatProperty step is in 1/100ths -> 0.5h per arrow click
        precision=1,
        # Deliberately no subtype='TIME' -- that ties into Blender's
        # scene-frame time display (e.g. HH:MM:SS), not a plain "hours
        # worked" number; the panel formats this as "X.Xh" itself instead.
        update=_on_timesheet_hours_update,
    )


def get_session_properties(context):
    return context.window_manager.kitsu

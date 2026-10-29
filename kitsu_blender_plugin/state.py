"""Module-level runtime session state, shared by every panel/operator.

Blender doesn't keep a persistent Panel *instance* around between redraws
the way Qt keeps a dock widget alive — `Panel.draw()` runs on a fresh,
throwaway instance every time. This module holds the equivalent of what
the Maya/Krita panel classes kept as `self.session`/`self.task`/etc, as one
process-wide singleton that every Panel's `draw()` reads and every
Operator's `execute()` writes.

None of this is a bpy.props-registered PropertyGroup: gazu returns plain
dicts and this holds a live KitsuSession object, neither of which bpy's
property system can represent (and both would be the wrong thing to save
into a .blend file even if it could). It also isn't meant to survive a
Blender restart — only the credentials in AddonPreferences are (see
preferences.py); a fresh Blender session always starts logged out and
re-authenticates automatically if a saved refresh token is present.
"""


class RuntimeState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.session = None  # kitsu_api.KitsuSession, or None if logged out
        self.status_message = ""
        self.status_is_error = False

        # Non-empty once the server has asked for a 2FA code (preferred
        # method first; see kitsu_api.TwoFactorRequired) — the Connection
        # panel only shows the code field while this is set.
        self.two_factor_methods = []

        self.current_user = None  # dict, from KitsuSession.current_user()
        self.user_avatar_path = None

        self.project = None  # dict, from KitsuSession.project()
        self.project_thumbnail_path = None

        self.task = None  # dict, from task_detection.detect_task()
        self.task_breadcrumb = []
        self.task_context = None  # "asset" | "shot" | None
        self.task_manual = False  # True when a remembered/hand-made pick (not the folders) resolved `task`
        self.task_statuses = []  # list of dicts, populated after login

        # The task picker (Episode -> Sequence -> Shot -> Task Type), shown
        # instead of just "No task detected." when detection can't resolve
        # the task (task_detection.ShotNotFound / TaskTypeMismatch), or any
        # time via Change Task. What it applies to: the asset/shot found by
        # detection or picked by hand, its names, and "asset"/"shot"
        # (picker_context stays None until a detection got that far).
        self.picker_entity = None
        self.picker_breadcrumb = None
        self.picker_context = None
        self.folder_task_type_name = None  # task-type folder name, for resolving a hand-picked shot
        self.task_picker_visible = False
        self.task_picker_hint = ""
        # Ids the picker's dropdowns were prefilled with, so "Use Selected"
        # only remembers a shot/task type the user actually changed (the
        # Nuke port's combo-changed signals give it that for free).
        self.picker_prefill_shot_id = None
        self.picker_prefill_task_type_id = None
        # task_detection.ShotTree for the picker, loaded lazily (bulk
        # requests) and reused while the project stays the same. Also the
        # source of the picker's dynamic enum items -- see properties.py.
        self.shot_tree = None
        self.shot_tree_project_id = None
        # The project's task types, for the picker's Task Type dropdown.
        self.task_type_options = []

        self.time_logged_today_minutes = 0
        self.timesheet_auto_advance = True
        self.timesheet_base_minutes = 0  # logged-today total the tracker started counting from
        self.timesheet_started_at = None  # time.time() when auto-advance (re)started

        self.comments = []  # list of raw gazu comment dicts, newest first
        self.selected_comment_index = -1
        self.comment_poll_registered = False

        # {comment_id: {preview_id: local_file_path}} — avoids re-downloading
        # the same preview/attachment/thumbnail repeatedly while the panel
        # is open.
        self.download_cache = {}

        self.publish_variations = []  # local file paths queued to publish (first = main preview, rest = variations)

    def is_logged_in(self):
        return self.session is not None

    def clear_session(self):
        """Drop everything tied to the current login (called on logout or
        a failed auto-login) but keep nothing else in memory stale."""
        self.reset()


runtime = RuntimeState()

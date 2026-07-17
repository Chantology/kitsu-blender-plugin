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

        self.current_user = None  # dict, from KitsuSession.current_user()
        self.user_avatar_path = None

        self.project = None  # dict, from KitsuSession.project()
        self.project_thumbnail_path = None

        self.task = None  # dict, from task_detection.detect_task()
        self.task_breadcrumb = []
        self.task_context = None  # "asset" | "shot" | None
        self.task_statuses = []  # list of dicts, populated after login

        # Set instead of `task` when detection resolved the asset/shot but
        # not its task-type folder name (task_detection.TaskTypeMismatch) --
        # the panel shows a manual task-type picker instead of "No task
        # detected." while this is set. `task_type_options` is the project's
        # task types, fetched alongside the mismatch so the dropdown has
        # something to show without another round-trip.
        self.task_type_mismatch = None
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

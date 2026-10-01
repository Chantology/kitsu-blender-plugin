"""Thin wrapper around the vendored `gazu` client for the plugin's needs.

All Kitsu/Zou protocol details (URL shape, auth headers, endpoint paths) are
handled by `gazu` itself. This module only adapts its calls to what the
plugin's UI needs and translates failures into specific, user-facing
messages instead of a generic "Login failed".

`gazu` and its own dependencies (`requests`, `urllib3`, `certifi`, `idna`)
are all vendored under vendor/ — Blender's bundled Python isn't expected to
have any of them. Importing them is deliberately deferred to
`KitsuSession.__init__` (i.e. until the user actually clicks Login), not
done at module import time, so that a problem here can never take down the
whole addon (panels + preferences) with it — only the login attempt fails,
with a clear message.
"""

import sys


class KitsuConnectionError(Exception):
    """A specific, user-facing reason a Kitsu/Zou call failed."""


# 2FA methods a code can be typed in for. Zou also supports "fido"
# (security keys), which needs a WebAuthn client this addon doesn't have —
# deliberately left out until someone actually needs it.
TWO_FACTOR_METHODS = ("totp", "email_otp", "recovery_code")


class TwoFactorRequired(KitsuConnectionError):
    """The email/password were accepted but the account has 2FA enabled —
    log in again with a code for one of `methods` (a subset of
    TWO_FACTOR_METHODS, `preferred` first). For "email_otp" the code has
    already been emailed by the time this is raised."""

    def __init__(self, message, methods, preferred):
        super().__init__(message)
        self.methods = methods
        self.preferred = preferred


class WrongTwoFactorCode(KitsuConnectionError):
    """The 2FA code was rejected (wrong, expired or already used). The
    methods from the preceding TwoFactorRequired still apply."""


def two_factor_prompt(method):
    """The status line asking for a code of the given 2FA method."""
    return {
        "totp": "Enter the code from your authenticator app, then log in again.",
        "email_otp": "A code was sent to your email — enter it, then log in again.",
        "recovery_code": "Enter one of your Kitsu recovery codes, then log in again.",
    }.get(method, "Enter your two-factor code, then log in again.")


def _import_gazu():
    try:
        from . import _vendor  # side effect: prioritizes vendor/ on sys.path
        _drop_stale_gazu_from_cache(_vendor._VENDOR_DIR)
        import gazu
    except Exception as exc:
        raise KitsuConnectionError(
            "Could not load the bundled gazu Kitsu/Zou client "
            f"(vendor/ folder may be missing or incomplete): {exc}"
        ) from exc
    return gazu


def _drop_stale_gazu_from_cache(vendor_dir):
    """Force the next `import gazu` to resolve from `vendor_dir` even if a
    DIFFERENT `gazu` is already cached in `sys.modules` from earlier in this
    same Blender session — e.g. a Login attempt made before this addon's own
    vendor/ folder was prioritized on sys.path (an older install, or before
    a fix like this one), or another addon's own gazu imported first for any
    reason.

    `_vendor`'s sys.path fix alone isn't enough to undo this: Python's
    import system checks `sys.modules` before it ever consults `sys.path`,
    so once something is cached there under the name "gazu", every later
    `import gazu` in the same process keeps returning that same (possibly
    wrong/incompatible) module — fixing sys.path only changes what gets
    found the *first* time "gazu" is imported. Only restarting Blender, or
    dropping the stale cache entries here, actually clears it.
    """
    existing = sys.modules.get("gazu")
    if existing is None:
        return
    if vendor_dir in (getattr(existing, "__file__", "") or ""):
        return  # already the right one — nothing to do
    for name in [n for n in sys.modules if n == "gazu" or n.startswith("gazu.")]:
        del sys.modules[name]


def _sum_durations(entry):
    """Coerce a `gazu.task.get_time_spent` per-person entry — of *whatever*
    shape it actually turns out to be (a plain number, a single dict with
    a "duration"/"total" key, or a list of either) — into one plain
    number. Anything unrecognized contributes 0 rather than raising, so a
    display bug here can never crash the UI it feeds."""
    if entry is None:
        return 0
    if isinstance(entry, (int, float)):
        return entry
    if isinstance(entry, dict):
        return entry.get("duration") or entry.get("total") or 0
    if isinstance(entry, list):
        return sum(_sum_durations(item) for item in entry)
    return 0


def normalize_host(url):
    """Turn a user-entered server URL into the `.../api` host gazu expects."""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise KitsuConnectionError("Server URL is empty.")
    if not url.endswith("/api"):
        url = f"{url}/api"
    return url


class KitsuSession:
    """One authenticated connection to a Kitsu/Zou instance."""

    def __init__(self, host):
        self.host = normalize_host(host)
        self.gazu = _import_gazu()
        # use_refresh_token=True so gazu silently mints a fresh access token
        # from the refresh token whenever a request hits a 401 (see gazu's
        # own _handle_auth_expiry) instead of failing the instant the
        # short-lived access token expires mid-session. This is gazu's own
        # opt-in behavior (its create_client() defaults it OFF, unlike
        # KitsuClient itself) — without it, the refresh token below is only
        # useful at the *next* login, not within this one.
        self.client = self.gazu.client.create_client(self.host, use_refresh_token=True)

    def login(self, email, password, two_factor_method=None, two_factor_code=None):
        """Log in with email/password, plus a 2FA code once the server has
        asked for one (see TwoFactorRequired).

        Posts to auth/login directly instead of via `gazu.log_in`: gazu
        turns Zou's 400 into an exception that keeps only the message text,
        dropping the `missing_OTP` / `wrong_OTP` flags and the list of the
        account's 2FA methods that decide whether a code field is needed at
        all. Same reasoning as update_comment_checklist below.
        """
        payload = {"email": email, "password": password}
        if two_factor_method and two_factor_code:
            payload[two_factor_method] = two_factor_code.strip()

        path = "auth/login"
        url = self.gazu.client.get_full_url(path, client=self.client)
        try:
            response = self.client.session.post(url, json=payload)
        except Exception as exc:
            raise KitsuConnectionError(f"Could not reach {self.host}: {exc}") from exc

        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = {}
        status = response.status_code

        if 200 <= status < 300 and body.get("login") is not False and body.get("access_token"):
            self.gazu.client.set_tokens(body, client=self.client)
            return

        if body.get("missing_OTP"):
            enabled = body.get("two_factor_authentication_enabled") or []
            methods = [m for m in TWO_FACTOR_METHODS if m in enabled]
            if not methods:
                raise KitsuConnectionError(
                    "This account's two-factor method isn't supported here. "
                    "Enable an authenticator app or email codes in Kitsu."
                )
            preferred = body.get("preferred_two_factor_authentication")
            if preferred not in methods:
                # e.g. a security-key-only account: only a recovery code is left.
                preferred = methods[0]
            if preferred == "email_otp":
                self.send_email_code(email)
            raise TwoFactorRequired(two_factor_prompt(preferred), methods, preferred)

        if body.get("wrong_OTP"):
            raise WrongTwoFactorCode("That code was wrong or has expired — try again.")

        if status == 404:
            raise KitsuConnectionError(
                f"{self.host} has no route at '{path}' (404). The server URL "
                "is likely wrong for the API — the Zou API may live at a "
                "different host or path than the Kitsu web UI. Ask whoever "
                "manages the server for the correct API URL."
            )
        if status == 403:
            raise KitsuConnectionError(
                f"{self.host} refused the request (403): {body.get('message') or path}"
            )
        if status >= 500:
            raise KitsuConnectionError(
                f"{self.host} returned a server error (HTTP {status}): "
                f"{body.get('message') or response.text[:200]}"
            )
        raise KitsuConnectionError(
            f"{self.host} rejected the login — check the email/password."
        )

    def send_email_code(self, email):
        """Have Kitsu email a fresh 2FA code (for "Resend code")."""
        try:
            self.gazu.send_email_otp(email, client=self.client)
        except Exception as exc:
            raise KitsuConnectionError(
                f"Could not ask {self.host} to email a code: {exc}"
            ) from exc

    def login_with_token(self, refresh_token):
        """Authenticate using a refresh token saved from a previous
        email/password login (see get_refresh_token) instead of typing the
        password again.

        Refresh tokens are longer-lived than the access token a normal login
        produces, but they still expire eventually (how long depends on the
        server's own configuration) — so this mints a fresh access token
        right away via gazu's own refresh endpoint, which both makes the
        session immediately usable and validates the token now rather than
        deferring a confusing failure to whatever the first real API call
        happens to be.
        """
        self.gazu.client.set_tokens(
            {"access_token": None, "refresh_token": refresh_token},
            client=self.client,
        )
        try:
            self.client.refresh_access_token()
        except self.gazu.exception.NotAuthenticatedException as exc:
            raise KitsuConnectionError(
                "The saved token has expired or was revoked — log in with "
                "your email and password once to get a new one."
            ) from exc
        except Exception as exc:
            raise KitsuConnectionError(f"Could not use the saved token: {exc}") from exc

    def get_refresh_token(self):
        """The refresh token gazu stored on this session's client after a
        successful email/password login — save this to skip the password
        next time (see login_with_token). None if this session logged in
        with a token to begin with (there's nothing new to save)."""
        return self.client.refresh_token

    def current_user(self):
        return self.gazu.client.get_current_user(client=self.client)

    def get_person(self, person_id):
        return self.gazu.person.get_person(person_id, client=self.client)

    def download_person_avatar(self, person, file_path):
        """Download a person's avatar/thumbnail. Raises if the person has
        none set on Kitsu — callers should treat that as "no avatar" rather
        than an error worth surfacing."""
        return self.gazu.files.download_person_avatar(
            person, file_path, client=self.client
        )

    def project(self, project_id):
        return self.gazu.project.get_project(project_id, client=self.client)

    def download_project_thumbnail(self, project, file_path):
        """Download a project's avatar/thumbnail. Raises if the project has
        none set — callers should treat that as "no thumbnail" rather than
        an error worth surfacing."""
        return self.gazu.files.download_project_avatar(
            project, file_path, client=self.client
        )

    def all_task_statuses(self, project):
        return self.gazu.task.all_task_statuses_for_project(project, client=self.client)

    def get_time_spent_today(self, task, date, person_id):
        """Minutes `person_id` has already logged today on this task, as a
        plain int/float (never a dict/list/None) — always safe to
        divide/multiply.

        Uses `gazu.task.get_time_spent`, which is explicitly documented as
        returning "a dict with person ID as key and time spent object as
        value" — but "time spent object" turned out, against a real
        server, to actually be a *list* of time-spent entries for that
        person/day (not a single dict, and not a number either), so this
        sums whatever's in there via `_sum_durations` rather than assuming
        one specific shape. The exact Zou schema here has surprised this
        code twice already (see README's "Verification status" section),
        so treat any future change to it the same way: widen
        `_sum_durations`, don't special-case a single new shape on top of
        the old wrong assumption.
        """
        result = self.gazu.task.get_time_spent(task, date, client=self.client) or {}
        return _sum_durations(result.get(person_id))

    def set_time_spent(self, task, person, date, duration_minutes):
        """Set (not add to) the total time `person` has logged on `task`
        for `date` — matching a slider that represents "today's total
        hours", where submitting should overwrite, not accumulate."""
        return self.gazu.task.set_time_spent(
            task, person, date, duration_minutes, client=self.client
        )

    def all_asset_types_for_project(self, project):
        return self.gazu.asset.all_asset_types_for_project(project, client=self.client)

    def get_asset_by_name(self, project, name, asset_type=None):
        return self.gazu.asset.get_asset_by_name(
            project, name, asset_type=asset_type, client=self.client
        )

    def all_task_types_for_project(self, project):
        return self.gazu.task.all_task_types_for_project(project, client=self.client)

    def get_task_by_entity(self, entity, task_type):
        # Works for any entity — asset or shot — so shot task resolution
        # reuses it unchanged.
        return self.gazu.task.get_task_by_entity(entity, task_type, client=self.client)

    def get_episode_by_name(self, project, name):
        return self.gazu.shot.get_episode_by_name(project, name, client=self.client)

    def get_sequence_by_name(self, project, name, episode=None):
        # `episode` scopes the lookup for TV shows, where the same sequence
        # name can recur across episodes; it's None (project-wide) for
        # movies/shorts/commercials.
        return self.gazu.shot.get_sequence_by_name(
            project, name, episode=episode, client=self.client
        )

    def get_shot_by_name(self, sequence, name):
        return self.gazu.shot.get_shot_by_name(sequence, name, client=self.client)

    def all_comments_for_task(self, task):
        return self.gazu.task.all_comments_for_task(task, client=self.client)

    def update_comment_checklist(self, comment_id, checklist):
        """Persist a comment's checklist (with updated "checked" flags).

        Sends a body with ONLY `checklist` (no "id") — confirmed against
        Zou's own server source and Kitsu's own frontend source that this
        exactly matches the wire payload Kitsu's own web UI sends for a
        checklist-only toggle. This alone was NOT enough to resolve a
        real report of a non-author (an artist assigned to the task,
        confirmed on the project team, against a Zou server confirmed
        newer than the version that added the "assigned team member can
        toggle checklist" exception) still getting denied — every
        condition checkable from Zou's public source said this should
        succeed. Since gazu's own exception classes only preserve the
        bare URL path and discard the response body on failure, this
        bypasses `gazu.client.put()`/`gazu.task.update_comment()` and
        makes the request directly, so a future failure surfaces Zou's
        actual JSON error body (which almost always has far more detail
        than a 404/403 status code alone) instead of just a route string.
        """
        url = self.gazu.client.get_full_url(f"data/comments/{comment_id}", client=self.client)
        headers = self.client.make_auth_header()
        headers["Content-Type"] = "application/json"
        try:
            response = self.client.session.put(
                url, json={"checklist": checklist}, headers=headers
            )
        except Exception as exc:
            raise KitsuConnectionError(
                f"Could not reach {self.host} to update the checklist: {exc}"
            ) from exc

        if 200 <= response.status_code < 300:
            return response.json()

        try:
            body = response.json()
        except ValueError:
            body = response.text
        raise KitsuConnectionError(
            f"Kitsu rejected this checklist update — HTTP {response.status_code} "
            f"on PUT {url}. Server response: {body}"
        )

    def publish_preview(self, task, task_status, comment="", preview_file_path=None, revision=None):
        """Publish a comment with a proper Kitsu preview/revision attached.

        Unlike a plain comment attachment, this shows up in the task's
        revision history (comparable across versions, drawover-able in
        review sessions). `revision` pins the revision number explicitly
        (e.g. parsed from a "_v002" filename) instead of letting Kitsu's
        counter auto-increment.
        """
        return self.gazu.task.publish_preview(
            task,
            task_status,
            comment=comment,
            preview_file_path=preview_file_path,
            revision=revision,
            client=self.client,
        )

    def add_comment(self, task, task_status, comment=""):
        """Post a status change + comment with no preview file attached —
        used when nothing was explicitly queued to publish, so a status
        change (e.g. "starting work") never implicitly captures and
        publishes a viewport snapshot nobody asked to share."""
        return self.gazu.task.add_comment(
            task, task_status, comment=comment, client=self.client
        )

    def add_extra_preview(self, task, comment, preview_file, file_path):
        """Add another preview file to `comment`, sharing the same revision
        as `preview_file` — a second (or third, ...) *variation* of the
        same publish (e.g. a different layer/render pass), as opposed to a
        new revision or a plain comment attachment."""
        extra_preview_file = self.gazu.task.create_extra_preview(
            task, comment, preview_file, client=self.client
        )
        return self.gazu.task.upload_preview_file(
            extra_preview_file, file_path, client=self.client
        )

    def download_attachment(self, attachment_file, file_path):
        return self.gazu.files.download_attachment_file(
            attachment_file, file_path, client=self.client
        )

    def download_preview(self, preview_file, file_path):
        return self.gazu.files.download_preview_file(
            preview_file, file_path, client=self.client
        )

    def download_preview_thumbnail(self, preview_file, file_path):
        """Download a preview's thumbnail PNG — used for video revisions,
        where downloading the whole movie just to show something inline
        would be wasteful."""
        return self.gazu.files.download_preview_file_thumbnail(
            preview_file, file_path, client=self.client
        )

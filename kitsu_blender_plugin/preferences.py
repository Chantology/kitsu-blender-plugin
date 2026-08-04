"""Persisted Kitsu connection settings.

Direct analog of the Maya/Houdini/Krita ports' `settings.py` (QSettings) —
same flat key set (server_url, email, project_id, api_token) — but stored
via Blender's own `AddonPreferences`, which persists in Blender's user
preferences independent of any open .blend file, exactly like QSettings
did. No separate JSON side-file is used.

`api_token` doubles as both the field the user can paste a token into and
the field Login overwrites with a fresh refresh token after a successful
email/password login — one field, exactly like the other three ports'
"api_token" QSettings key (there is deliberately no separate
"refresh_token" key: that would let a user-pasted value and an
auto-saved one silently disagree about which one Login actually uses).

The account **password is never stored here** — it only ever exists as a
transient property on the Login operator itself (see operators.py),
matching "password never written to disk" in all three prior ports.
"""

import bpy


class KitsuAddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    server_url: bpy.props.StringProperty(
        name="Server URL",
        description="Kitsu/Zou server, e.g. https://kitsu.yourstudio.com "
        "(normalized to end in /api automatically)",
        default="",
    )
    email: bpy.props.StringProperty(
        name="Email",
        description="Kitsu account email",
        default="",
    )
    project_id: bpy.props.StringProperty(
        name="Project ID",
        description="Paste the project ID from its Kitsu URL",
        default="",
    )
    api_token: bpy.props.StringProperty(
        name="API Token",
        description="Optional: a saved refresh token, used instead of a "
        "password. Filled in automatically after a successful "
        "email/password login",
        default="",
    )
    preview_render_time_limit: bpy.props.IntProperty(
        name="Preview Render Time Limit (s)",
        description="When a capture has to fall back to a real render "
        "('Rendered' viewport shading on Cycles), stop sampling after this "
        "many seconds instead of rendering the scene's full sample count. "
        "Blender is unresponsive while it renders, so this bounds the wait. "
        "0 renders at full quality with no limit. Only affects Kitsu "
        "captures — the scene's own render settings are left untouched",
        default=30,
        min=0,
        soft_max=600,
    )

    def draw(self, context):
        layout = self.layout
        layout.label(
            text="These can also be edited from the Kitsu panel's "
            "Connection section (3D Viewport sidebar, N-panel)."
        )
        layout.prop(self, "server_url")
        layout.prop(self, "email")
        layout.prop(self, "project_id")
        layout.prop(self, "api_token")
        layout.separator()
        layout.prop(self, "preview_render_time_limit")


def ensure_addon_entry():
    """Make sure this addon has an entry in `preferences.addons`.

    Blender only creates that entry when an addon is enabled with
    `default_set=True` — what ticking the box in Preferences > Add-ons
    does. The `--addons` command-line flag, which the studio launch configs
    use to enable this addon at startup, enables with `default_set=False`
    instead: the module is imported and register() runs (so the Kitsu tab
    shows up), but no preferences entry is ever created.

    Without an entry, `preferences.addons[__package__]` raises KeyError in
    every draw() and operator that needs a setting. Blender swallows the
    exception from a Panel.draw() and renders the header with an empty
    body, so the symptom is a Kitsu tab whose "Connection" section looks
    stuck and unopenable, plus an unticked checkbox in Preferences.

    Creating the entry here also makes the addon show as enabled in
    Preferences > Add-ons, so with Blender's default "Auto-Save
    Preferences" it stays enabled on later launches.
    """
    addons = bpy.context.preferences.addons
    if addons.get(__package__) is not None:
        return
    addons.new().module = __package__


def get_preferences(context):
    # Defensive fallback: register() calls ensure_addon_entry() so the entry
    # normally exists well before anything draws, but never let a missing
    # entry turn into a KeyError that blanks out the whole panel.
    addon = context.preferences.addons.get(__package__)
    if addon is None:
        ensure_addon_entry()
        addon = context.preferences.addons[__package__]
    return addon.preferences

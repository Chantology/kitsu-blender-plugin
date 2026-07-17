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


def get_preferences(context):
    return context.preferences.addons[__package__].preferences

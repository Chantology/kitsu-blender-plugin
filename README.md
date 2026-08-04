# Kitsu Blender Plugin

A Blender 5.2 add-on that connects to a self-hosted Kitsu/Zou (CG Wire)
instance: it detects the task a `.blend` file belongs to from its folder
structure, shows feedback/comments (including drawover attachments), and
publishes new versions with a comment and status change.

This is the Blender counterpart of the
[Kitsu Maya Plugin](../kitsu-maya-plugin) (and its
[Houdini](../kitsu-houdini-plugin) and [Krita](../kitsu-krita-plugin)
siblings) — same features, same Kitsu-side behavior, but with the UI
rebuilt from scratch on Blender's own `Panel`/`Operator` system rather than
ported from Qt: **Blender has no bundled Qt/PySide**, so the dockable Qt
panel the other three hosts use isn't something this port can reuse. What
you get instead is a **"Kitsu" tab in the 3D Viewport's sidebar** (press
`N`), built entirely with native Blender widgets.

All Kitsu/Zou protocol handling (auth, endpoints, uploads/downloads) is
delegated to CGWire's official [`gazu`](https://github.com/cgwire/gazu)
Python client, exactly like the other three ports. Blender's bundled
Python doesn't have `gazu` or `requests`, so both are vendored inside
`kitsu_blender_plugin/vendor/` along with `requests`'s own dependencies
(`urllib3`, `certifi`, `idna`). No manual `pip install` into Blender's
Python is required.

Built and tested against Blender's own bundled Python (3.13 on 5.2 LTS).
Note: **this repository's own development/verification was done against
locally available Blender 4.1/4.2/4.4 builds** — no Blender 5.2 build was
available in that environment. Every 5.2-specific API call (the FFmpeg
`media_type` field, in particular) is written with a defensive fallback to
the older API shape it replaced, and the rest of the addon uses long-
stable `bpy` API surface, but a final pass against a real Blender 5.2
install is recommended before production use — see "Verification status"
below for exactly what was and wasn't exercised.

## Differences from the Maya/Houdini/Krita versions

Every feature from the other three ports is present here — nothing is
dropped — but a few are implemented differently because Blender's own
capabilities are different:

- **The whole UI** is native Blender `Panel`/`Operator`/`PropertyGroup`
  code, not a ported Qt dock. Settings persistence uses Blender's own
  `AddonPreferences` (the direct analog of the other ports' `QSettings`)
  instead of a custom dialog; section collapsing needs no custom widget at
  all, since Blender's own Panel headers are already click-to-collapse.
- **Large images** (drawover-composited revisions, attachments) open in a
  separate **Image Editor area** rather than inline in the panel — Blender
  has no widget that embeds a full-size raster image directly inside a
  Panel (only icon-sized previews, via `bpy.utils.previews`, which this
  addon uses for avatars/thumbnails/attachment icons where a small preview
  is enough).
- **Drawover rendering** (Kitsu's Fabric.js shapes — rectangles, circles,
  lines, arrows, text, freehand pencil strokes) is reimplemented from
  scratch on top of Blender's own `numpy` (bundled with Blender's Python)
  for shape rasterization, plus `gpu`/`blf` specifically for text (numpy
  has no font rendering of its own). The Maya/Houdini/Krita ports do this
  with `QPainter`, which isn't available here at all.
- **Viewport capture** uses `bpy.ops.render.opengl` (Blender's own
  OpenGL/viewport render operator) instead of a Maya playblast or Houdini
  flipbook. Overlays and gizmos are hidden for the duration, so a preview
  shows the shot rather than the working viewport (grid, selection outlines).
  Captures inherit the viewport's live shading — with one exception Blender
  itself imposes: **'Rendered' shading with an add-on render engine (Cycles,
  or any third-party engine) cannot be captured from the viewport at all**
  (Blender's external-engine draw callback is a no-op with no interactive
  context, so the frame comes out as bare overlays over the theme
  background). A single-frame capture in that state falls back to a real
  render instead — bounded by the **Preview Render Time Limit** add-on
  preference (default 30s, 0 for unlimited), because Blender's UI is frozen
  for the whole render: an unbounded capture of a production lookdev asset at
  514 samples measured ~22 minutes. The limit caps Cycles sampling only, so a
  heavy scene still pays for its own mesh/hair evaluation and BVH build before
  the first sample. A full-range video capture refuses with an explanation
  rather than rendering for hours or publishing unshaded frames. Solid and
  Material Preview are drawn by Blender's own internal engines and work
  normally on any render engine.
- **Full-scene video export** ("Publish Capture") is actually *simpler*
  here than on every other host: Blender's own render pipeline links
  ffmpeg directly, so a full-range video is written in one step with no
  external `ffmpeg` binary required on PATH at all (the Houdini port needs
  one; Blender doesn't).
- **Camera background image** (the drawover-overlay feature) uses
  `Camera.background_images` instead of a Maya image plane; a per-frame
  animated drawover sequence uses a `MovieClip` datablock (Blender already
  re-reads whichever numbered file matches the current frame on its own —
  no custom per-frame expression/driver needed, unlike the Maya port).
- **Timeline markers** use `scene.timeline_markers` instead of Maya's Time
  Slider Bookmarks. Blender's markers have no color property, so unlike
  Houdini's colored bookmark bands, the revision/frame identifying
  information is encoded in the marker's name only (e.g. `Kitsu r5 f120`).

## Folder layout

Everything under `kitsu_blender_plugin/` is exactly what gets copied into
Blender's addons folder:

```
kitsu_blender_plugin/
  __init__.py              # bl_info, class registration, register()/unregister()
  preferences.py           # AddonPreferences: server URL/email/project ID/API token
  state.py                 # runtime session state (session, task, comments, ...)
  properties.py            # the few UI-bound values (comment text, status, timesheet hours)
  async_utils.py           # background-thread HTTP helper + bpy.app.timers poller
  kitsu_api.py             # wraps vendored gazu for login/tasks/comments
  task_detection.py        # resolves asset/shot task from folder structure
  versioning.py            # parses "_v###" filename versions
  capture_naming.py        # studio review-file naming convention for Publish Capture
  video_annotations.py     # per-frame drawover extraction for video previews
  annotations_raster.py    # renders Kitsu drawovers (numpy + gpu/blf for text)
  blender_scene.py         # scene path, viewport/video capture, camera background, markers
  thumbnails.py            # bpy.utils.previews-backed icon cache (avatars, thumbnails)
  image_popup.py           # opens a downloaded image in an Image Editor area
  paths.py                 # per-session scratch directory for downloads
  operators.py             # every button the panels expose
  panels.py                # the "Kitsu" sidebar tab: Connection/Task/Feedback/Publish
  vendor/
    gazu/                  # vendored CGWire gazu client
    requests/, urllib3/,   # vendored gazu runtime dependencies
    certifi/, idna/
    typing_extensions.py   # shim (Blender's Python already has typing.Literal)
```

## Installation

### Automatically

Run the installer script for your OS from the repository root — each
copies `kitsu_blender_plugin/` into every installed Blender version's
`scripts/addons` folder it can find (removing any previous install of the
plugin's own folder first, so nothing stale from an older version is left
behind):

- **Windows**: double-click `install_windows.cmd` (installs into every
  `%APPDATA%\Blender Foundation\Blender\<major>.<minor>` folder found).
- **macOS**: double-click `install_mac.command` (installs into every
  `~/Library/Application Support/Blender/<major>.<minor>` folder found). If
  it won't open ("not executable"), run `chmod +x install_mac.command`
  once in Terminal first.
- **Linux**: run `./install_linux.sh` in a terminal (`chmod +x
  install_linux.sh` first if needed). Installs into every
  `~/.config/blender/<major>.<minor>` folder found (also checks the
  Flatpak/Snap equivalents).

Blender keys its config folder by **major.minor** version, not by exact
build, so a single install already covers every point release of that
line — but a different major.minor (e.g. going from 5.2 to 5.3) needs its
own folder, which the installer picks up automatically as long as you've
launched that Blender version at least once (so it exists). If you use a
custom Blender config directory, pass that version folder as an argument
to the script instead, e.g. `install_linux.sh /path/to/your/blender/5.2`.

### Manually

1. Copy the `kitsu_blender_plugin/` folder into your Blender version's
   `scripts/addons` folder (create it if it doesn't exist):
   - Windows: `%APPDATA%\Blender Foundation\Blender\<major>.<minor>\scripts\addons\`
   - macOS: `~/Library/Application Support/Blender/<major>.<minor>/scripts/addons/`
   - Linux: `~/.config/blender/<major>.<minor>/scripts/addons/`
2. Restart Blender.
3. Go to **Edit > Preferences > Add-ons**, search "Kitsu", and enable it.
4. Open the **3D Viewport**, press **N** to open the sidebar, and click the
   **Kitsu** tab.

### If the add-on fails to load

Blender's **System Console** (Window > Toggle System Console on Windows;
launch Blender from a terminal on macOS/Linux) shows a traceback if
enabling the add-on fails. Since `gazu` and `requests` (plus their
dependencies) are fully vendored, the panel should appear regardless of
what's installed in Blender's own Python — importing them is deferred
until you actually click **Login**, so a problem there only fails the
login attempt with a specific message, not the whole add-on.

## Using the plugin

1. Open the **Kitsu** tab in the 3D Viewport's sidebar (`N`). In the
   **Connection** section, enter your server URL (e.g.
   `https://kitsu.yourstudio.com`), email, and project ID, then click
   **Login** — you'll be prompted for your password (or leave the **API
   Token** field filled in from a previous login to skip straight to it).
   - The server URL is normalized to end in `/api` automatically.
   - If login fails, the status line shows the *specific* reason (rejected
     credentials, a 404 route, a server error, etc.) instead of a generic
     failure.
   - After logging in with email/password, the API Token field is filled
     in automatically with a fresh refresh token — from then on, opening
     Blender with a server URL, project ID, and that token already saved
     logs in **automatically**, no click needed. If the token has expired,
     the automatic attempt just fails quietly into the status line, and
     logging in with your password once more replaces it with a fresh one.
   - Your avatar and a "Welcome, `<name>`" line appear once logged in.
   - **Edit > Preferences > Add-ons > Kitsu** also holds *Preview Render Time
     Limit* — how long a capture may spend rendering when the viewport is on
     Rendered shading with Cycles (default 30s; 0 renders at the scene's full
     sample count). Nothing else needs it; the connection fields are all
     editable from the sidebar.
2. Save your `.blend` file somewhere matching your project's folder
   convention, then open the **Task** section and click **Detect Task**.
   Both asset and shot contexts are supported:

   | Context | Expected folder layout above the file |
   | --- | --- |
   | Asset | `.../<AssetType>/<AssetName>/<TaskType>/file.blend` |
   | Shot (movie/short/commercial) | `.../<Sequence>/<Shot>/<TaskType>/file.blend` |
   | Shot (TV series, with episodes) | `.../<Episode>/<Sequence>/<Shot>/<TaskType>/file.blend` |

   Folder names are matched case-insensitively against Kitsu's own names.
   Once detected, the project thumbnail/name, the task breadcrumb, status,
   and due date are shown, along with a **timesheet**: a slider (0–12h in
   half-hour steps), 1h/4h/8h presets, and **Log Time** (sets today's
   total rather than adding to it). The slider also auto-advances in real
   time from whatever's already logged today — dragging it or clicking a
   preset pauses the auto-advance until the next task switch or successful
   **Log Time**.
3. The **Feedback** section lists comments for the detected task, newest
   first, auto-refreshing every 60 seconds. Click a comment to see its
   checklist (tickable, synced to Kitsu immediately), its revisions (with
   **View** to open a drawover-composited image in an Image Editor, or
   **Show as Camera Background** to show it on the active camera; video
   revisions get **Add Timeline Markers** and **Show Drawovers in
   Viewport** instead), and its attachments (**Save Attachment(s)** to
   write them to disk).
4. The **Publish** section captures the viewport at the current frame and
   publishes it as a new Kitsu revision — pick a status, write a comment,
   optionally queue extra images (**+ From Viewport** / **+ From
   File...**) as additional previews on the same revision, then click
   **Publish**. If the `.blend` filename ends in a `_v###`-style version,
   that number becomes the Kitsu revision number too.
5. **Publish Capture** (a sub-panel under Publish) exports the full
   playback range as an H.264 `.mp4` and publishes it as a new revision —
   no external `ffmpeg` needed, Blender's own render pipeline handles it.
   Saved to `<scene folder>/capture/<name>.mp4`, following the same
   `<Show>[_<Episode>]_<Sequence>_<Shot>_v<version>_<InitialLastname>.mp4`
   convention as the other ports (swap `capture_naming.py`'s
   `build_capture_filename` if your studio's convention differs).

### Drawover rendering limitations

Same limitations as the other three ports, since this reimplements the
same Fabric.js shape set: the eraser tool's mask, nested groups, and
rotation on freehand strokes/lines/arrows specifically are not supported
(rotation on rectangles/circles/text works). A shape that can't be drawn
is skipped rather than breaking the rest of the drawover.

## Verification status

This addon was built and smoke-tested against locally available Blender
4.1/4.2/4.4 (no 5.2 build was available in the development environment):

- **Verified**: add-on registration/unregistration (including repeated
  enable/disable cycles), `AddonPreferences` and `WindowManager` property
  access, every Connection/Task/Publish operator's error handling for
  not-logged-in/no-task/missing-viewport cases, and the `numpy`-based
  shape rasterizer in `annotations_raster.py` (rectangles, circles, lines,
  arrows all confirmed to render correctly pixel-for-pixel by inspection).
- **Logically verified but not pixel-confirmed**: the `gpu`/`blf` text
  rendering path in `annotations_raster.py`. It follows Blender's
  documented offscreen-rendering pattern (an explicit pixel-space
  orthographic projection matrix, `blf.draw` into a `GPUOffScreen`, read
  back and composited), but `gpu`/`blf` drawing functions are unavailable
  in Blender's `--background` mode ("GPU functions for drawing are not
  available in background mode") — the only mode scriptable in this
  environment. This is not a real-world limitation (the addon only ever
  runs in Blender's normal windowed mode, where a GPU context always
  exists), but it does mean text-annotation rendering specifically should
  get a manual visual check in a real Blender 5.2 session before relying
  on it.
- **Not testable at all in this environment**: `bpy.ops.render.opengl`
  (viewport/video capture) and `bpy.ops.wm.window_new` (Image Editor
  popup) both require a real window/OpenGL context and fail immediately
  under `--background` — confirmed to fail with a clear, expected error
  ("Cannot use OpenGL render in background mode"), not a bug, but also not
  something this environment could positively confirm working.

Recommended before production use: open the addon in a real Blender 5.2
session and walk through Login → Detect Task → Feedback (including a
comment with a text-shape drawover) → Publish → Publish Capture once,
against a real (or disposable test) Kitsu project.

"""Blender-side integration: everything that talks to `bpy` itself.

This is the Blender counterpart of the Maya port's `maya_scene.py` / the
Houdini port's `houdini_scene.py` — kept in one module so the rest of the
addon (panels, the Kitsu API wrapper, task detection, drawover rendering)
stays host-agnostic:

- the current scene's file path (for task detection + version parsing)
- a single-frame viewport capture (what gets published as the preview)
- a full playback-range video capture (for the "Publish Capture" feature)
- a camera background image showing a review drawover in the viewport
- timeline markers for frame-based video feedback

Two things are simpler here than on every other host this plugin has been
ported to: Blender's own render pipeline links ffmpeg directly (confirmed
for Blender 5.2 — no external `ffmpeg` binary on PATH is required, unlike
the Houdini port), and a per-frame drawover sequence needs no custom
per-frame expression/driver the way Maya's image plane did — a `MovieClip`
datablock already re-reads whichever numbered file matches the current
frame on its own.
"""

import contextlib
import glob
import os
import shutil
import tempfile

import bpy


class BlenderSceneError(Exception):
    """A specific, user-facing reason a Blender-side operation failed."""


def scene_path():
    """Absolute path of the current .blend file, or None if it was never saved."""
    return bpy.data.filepath or None


def load_task_type_override():
    """The manually-picked task-type id remembered for the current scene
    (see save_task_type_override), or None if none is set — task detection
    falls back to normal folder-based matching in that case.

    Stored as a Scene-registered property (see __init__.py), not
    WindowManager state like the rest of properties.py: this needs to be
    saved *into the .blend file itself* so the choice survives a Blender
    restart, unlike the transient session state in state.py."""
    return bpy.context.scene.kitsu_task_type_override_id or None


def save_task_type_override(task_type_id):
    """Remember a manually-picked task-type id on the current scene."""
    bpy.context.scene.kitsu_task_type_override_id = task_type_id


def scene_fps(scene=None):
    """Frames per second of the scene, matching Blender's own fps_base division."""
    scene = scene or bpy.context.scene
    return scene.render.fps / scene.render.fps_base


def playback_start(scene=None):
    """First frame of the playback range — frame 0 of a published capture."""
    scene = scene or bpy.context.scene
    return scene.frame_start


def playback_range(scene=None):
    """(start, end) frames of the current playback range, inclusive."""
    scene = scene or bpy.context.scene
    return (scene.frame_start, scene.frame_end)


def render_resolution(scene=None):
    """The scene's render resolution (width, height)."""
    scene = scene or bpy.context.scene
    return (scene.render.resolution_x, scene.render.resolution_y)


def _find_view3d_context():
    """A context override pointing at a visible VIEW_3D area/region, for
    `bpy.ops.render.opengl` calls made from an operator that might not
    itself be running with a 3D viewport as the active area (e.g. a
    popup dialog's confirm button) — plus that region's own RegionView3D
    (`region.data`), so callers can temporarily force it into true Camera
    View (see `_temporary_camera_view`). Returns (override_dict,
    region_3d), or (None, None) if no 3D Viewport is open anywhere.
    """
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            for region in area.regions:
                if region.type == "WINDOW":
                    return {"window": window, "area": area, "region": region}, region.data
    return None, None


@contextlib.contextmanager
def _temporary_camera_view(region_3d, space=None):
    """Force `region_3d` into true Camera View (`view_perspective =
    'CAMERA'`, the same state Numpad-0 produces) for the duration of the
    block, restoring whatever it was set to afterward.

    Both `view_context=True` and `view_context=False` on `render.opengl`
    output at the scene's configured render resolution regardless (this
    is not a viewport-pixel-size issue, confirmed by reading Blender's
    own OpenGL-render source) — but `view_context=True` renders *whatever
    the given viewport is currently looking at*, while `view_context=False`
    substitutes the scene's actual camera transform but ALSO hardcodes
    flat, unlit "solid" shading, bypassing the viewport's own Material
    Preview/Rendered/etc. shading entirely (confirmed from a real report:
    switching to view_context=False to fix a misalignment produced a flat
    grey cube despite the viewport being set to Material Preview the
    whole time).

    The actual fix for the original misalignment was never about
    view_context's shading-vs-framing tradeoff at all — it's that the
    viewport being captured wasn't necessarily looking through the
    scene's own camera. Locking it into Camera View first means
    `view_context=True` renders exactly the camera's own transform (same
    as a real render) while still inheriting that viewport's live shading
    settings, giving both at once instead of trading one for the other.
    Toggling `view_perspective` back afterward restores the viewport's
    previous free-look position/rotation automatically (the same as
    pressing Numpad-0 twice) — Blender keeps it in separate storage from
    the camera-locked state, so nothing about the user's own navigation
    is lost.

    Skipped entirely when there is no camera to look through (e.g. a
    modeling task's scene): Camera View without a camera makes Blender
    build the view from the viewport's rotation and distance alone,
    dropping its orbit pivot (`view_location`), so the capture comes out
    centered on the world origin instead of what the artist framed.
    Leaving the viewport's own free-look view in place is the right
    framing there.
    """
    has_camera = bpy.context.scene.camera is not None or (
        space is not None
        and getattr(space, "use_local_camera", False)
        and getattr(space, "camera", None) is not None
    )
    if region_3d is None or not has_camera:
        yield
        return
    previous = region_3d.view_perspective
    try:
        region_3d.view_perspective = "CAMERA"
    except Exception:
        yield
        return
    try:
        yield
    finally:
        try:
            region_3d.view_perspective = previous
        except Exception:
            pass


# Blender's own render engines — the only ones its OpenGL/viewport render can
# draw with (see `_needs_full_render`). Deliberately an allowlist of built-ins
# rather than a denylist of add-on engines: an add-on engine only shows up in
# `bpy.types.RenderEngine.__subclasses__()` while that add-on is actually
# loaded, so a session where Cycles never registered (it is not in the
# launcher's `--addons` list, and a machine whose preferences do not enable it
# will not load it) would report a scene's leftover engine="CYCLES" as "not an
# add-on engine" and take the broken viewport path. Testing against the
# built-ins instead means an unknown or unregistered engine id always errs
# toward a real render.
#
# BLENDER_EEVEE_NEXT is EEVEE's id on Blender 4.2-4.5 only; harmless to keep
# listed on 5.x, where the id is BLENDER_EEVEE again.
_INTERNAL_RENDER_ENGINES = {
    "BLENDER_EEVEE",
    "BLENDER_EEVEE_NEXT",
    "BLENDER_WORKBENCH",
}


def _addon_render_engine_ids():
    """Identifiers of the render engines registered by add-ons in this
    session — Cycles, plus any third-party engine (Octane, V-Ray...).

    Diagnostic only; `_needs_full_render` intentionally does not depend on it
    (see `_INTERNAL_RENDER_ENGINES` above). Note that introspecting
    `RenderSettings.bl_rna`'s `engine` enum is useless for this — it lists
    only the built-ins and omits Cycles even when Cycles is enabled and
    selected (verified on Blender 5.2).
    """
    return {
        cls.bl_idname
        for cls in bpy.types.RenderEngine.__subclasses__()
        if getattr(cls, "bl_idname", None)
    }


def _needs_full_render(scene, space):
    """Whether capturing `space` would need a real render instead of a
    viewport/OpenGL render to show what the artist actually sees.

    True only for the one combination Blender's OpenGL render cannot
    reproduce: 'Rendered' viewport shading with an add-on render engine.
    During `render.opengl` there is no interactive context, and Blender's
    external-engine draw callback is a no-op without one — so the add-on
    engine contributes nothing and the capture comes out as bare overlays
    (grid, selection outlines, gizmos) over the theme background, written
    instantly because no rendering ever happened. Confirmed on Blender 5.2:
    an identical scene captured with Cycles + Rendered produced only the
    cube's outline, while EEVEE + Rendered produced a correctly shaded cube
    and the world background.

    Solid and Material Preview are unaffected on any engine — both are drawn
    by Blender's own internal engines (Workbench / EEVEE), which do work in
    the offscreen context, so those keep the fast viewport-render path.
    """
    shading = getattr(getattr(space, "shading", None), "type", None)
    return shading == "RENDERED" and scene.render.engine not in _INTERNAL_RENDER_ENGINES


@contextlib.contextmanager
def _temporary_clean_viewport(space):
    """Hide the captured viewport's overlays and gizmos for the duration of
    the block, restoring them afterward.

    Blender's viewport render honours the viewport's own overlay settings, so
    without this a published preview carries whatever the artist happened to
    have switched on — floor grid, orange selection outlines, the transform
    gizmo, relationship lines. Reviewers in Kitsu should see the shot, not
    the working viewport.
    """
    overlay = getattr(space, "overlay", None)
    saved_overlays = getattr(overlay, "show_overlays", None)
    saved_gizmo = getattr(space, "show_gizmo", None)
    try:
        if saved_overlays is not None:
            overlay.show_overlays = False
        if saved_gizmo is not None:
            space.show_gizmo = False
        yield
    finally:
        if saved_overlays is not None:
            overlay.show_overlays = saved_overlays
        if saved_gizmo is not None:
            space.show_gizmo = saved_gizmo


@contextlib.contextmanager
def _temporary_cycles_time_limit(scene, seconds):
    """Cap a Cycles render at `seconds` for the duration of the block.

    Cycles' own `time_limit` (Render Properties > Sampling > Render > Time
    Limit): sampling stops when the budget runs out and the result is
    denoised and returned as normal, so a capture costs a predictable wait
    instead of the scene's full sample count. Measured on a production
    lookdev asset at 514 samples, an unbounded capture took ~22 minutes of
    completely frozen Blender.

    Bounds *sampling only* — scene sync, mesh/hair evaluation and BVH build
    happen before the first sample and are not covered, so a heavy scene
    still costs whatever its sync costs. `seconds` of 0/None means no limit,
    and engines other than Cycles have no equivalent setting, so both are
    no-ops here.
    """
    cycles = getattr(scene, "cycles", None)
    if not seconds or cycles is None or not hasattr(cycles, "time_limit"):
        yield
        return
    saved = cycles.time_limit
    cycles.time_limit = float(seconds)
    try:
        yield
    finally:
        cycles.time_limit = saved


def _set_still_image_output(render):
    """Point `render`'s output settings at a single PNG still.

    `media_type` has to be set before `file_format` on Blender 5.x: with
    media_type='VIDEO' (which is what a scene configured to output an FFmpeg
    movie has), `file_format` only accepts 'FFMPEG' and assigning "PNG"
    raises outright — so a capture from such a scene would fail before it
    ever rendered. Both are saved and restored by
    `_temporary_render_settings`, so this only applies for the capture.
    """
    image_settings = render.image_settings
    if hasattr(image_settings, "media_type"):
        image_settings.media_type = "IMAGE"
    image_settings.file_format = "PNG"


@contextlib.contextmanager
def _temporary_render_settings(scene):
    """Save/restore the handful of render settings this module touches, so
    capturing a viewport never permanently changes the user's own render
    output configuration."""
    render = scene.render
    saved = {
        "filepath": render.filepath,
        "resolution_percentage": render.resolution_percentage,
    }
    image_settings = render.image_settings
    saved_image = {"file_format": image_settings.file_format}
    if hasattr(image_settings, "media_type"):
        saved_image["media_type"] = image_settings.media_type
    ffmpeg_saved = None
    if hasattr(render, "ffmpeg"):
        ffmpeg = render.ffmpeg
        ffmpeg_saved = {
            "format": ffmpeg.format,
            "codec": ffmpeg.codec,
            "constant_rate_factor": ffmpeg.constant_rate_factor,
        }
    try:
        yield
    finally:
        render.filepath = saved["filepath"]
        render.resolution_percentage = saved["resolution_percentage"]
        # media_type must be restored BEFORE file_format: on Blender 5.x,
        # image_settings.file_format's valid enum values depend on the
        # current media_type (with media_type='VIDEO', file_format only
        # accepts 'FFMPEG') -- restoring file_format first would try to set
        # e.g. "PNG" while media_type is still "VIDEO" from the capture,
        # which Blender rejects outright.
        if "media_type" in saved_image:
            image_settings.media_type = saved_image["media_type"]
        if "file_format" in saved_image:
            image_settings.file_format = saved_image["file_format"]
        if ffmpeg_saved is not None:
            for key, value in ffmpeg_saved.items():
                setattr(render.ffmpeg, key, value)


def capture_viewport(target_path, render_time_limit=None):
    """Capture the current frame of the active viewport to `target_path`.

    `render_time_limit` (seconds) only applies to the full-render fallback
    below, and only on Cycles — see `_temporary_cycles_time_limit`. None or 0
    renders at the scene's own full quality.

    Uses Blender's OpenGL/viewport render (`render.opengl`) rather than a
    full renderer so what gets published is exactly what's on screen,
    including HUD-free shading — the Blender equivalent of Maya's
    playblast / Houdini's flipbook / Krita's canvas export.

    `view_context=True`, with the captured viewport forced into true
    Camera View for the duration (see `_temporary_camera_view`) — this
    keeps the viewport's own live shading (Material Preview/Rendered/
    Solid+color/etc.) while still framing exactly through the scene's
    actual camera, matching what a real render sees.

    The one shading mode the viewport render cannot reproduce — 'Rendered'
    with an add-on engine such as Cycles — falls back to a real render
    instead (see `_needs_full_render` and `_render_still_with_engine`).
    """
    scene = bpy.context.scene
    target_dir = os.path.dirname(target_path) or "."
    if not os.path.isdir(target_dir):
        raise BlenderSceneError(f"Output folder does not exist: {target_dir}")

    override, region_3d = _find_view3d_context()
    if override is None:
        raise BlenderSceneError(
            "No 3D Viewport is visible — make sure a Viewport is open "
            "before capturing."
        )

    space = override["area"].spaces.active
    if _needs_full_render(scene, space):
        return _render_still_with_engine(scene, target_path, render_time_limit)

    # Blender's write_still always appends a frame-number suffix to
    # render.filepath (e.g. "capture0001.png"), so — exactly like the Maya
    # port's playblast handling — capture into a private temp dir and move
    # whatever file actually gets produced to the exact requested path,
    # rather than predicting Blender's own naming.
    capture_dir = tempfile.mkdtemp(prefix="kitsu_capture_")
    try:
        with _temporary_render_settings(scene), _temporary_camera_view(
            region_3d, space
        ), _temporary_clean_viewport(space):
            scene.render.filepath = os.path.join(capture_dir, "capture")
            _set_still_image_output(scene.render)
            with bpy.context.temp_override(**override):
                result = bpy.ops.render.opengl(write_still=True, view_context=True)
            if result != {"FINISHED"}:
                raise BlenderSceneError("Viewport capture did not complete.")

        produced = sorted(glob.glob(os.path.join(capture_dir, "capture*")))
        if not produced:
            raise BlenderSceneError(
                "Viewport capture produced no image — make sure a 3D "
                "Viewport is visible."
            )
        shutil.move(produced[0], target_path)
    finally:
        shutil.rmtree(capture_dir, ignore_errors=True)

    return target_path


def _render_still_with_engine(scene, target_path, time_limit=None):
    """Render the current frame with the scene's actual render engine and
    write it to `target_path`.

    The fallback for 'Rendered' viewport shading on an add-on engine, where
    a viewport render would silently produce an unshaded frame (see
    `_needs_full_render`). This is a real render, so it takes as long as one
    and blocks Blender's UI while it runs — `bpy.ops.render.render` is called
    with the default (blocking) execution context deliberately: an
    INVOKE_DEFAULT render runs modally and would return before the file
    exists.

    Framing needs no viewport handling here — a real render always looks
    through the scene camera, which is the same framing the viewport-render
    path goes out of its way to reproduce.
    """
    if scene.camera is None:
        raise BlenderSceneError(
            f"The viewport is set to Rendered shading with the "
            f"{scene.render.engine} engine, which has to be captured as a "
            "real render, but the scene has no active camera. Set a camera, "
            "or switch the viewport to Material Preview."
        )

    capture_dir = tempfile.mkdtemp(prefix="kitsu_render_")
    try:
        with _temporary_render_settings(scene), _temporary_cycles_time_limit(
            scene, time_limit
        ):
            scene.render.filepath = os.path.join(capture_dir, "capture")
            _set_still_image_output(scene.render)
            result = bpy.ops.render.render(write_still=True)
            if result != {"FINISHED"}:
                raise BlenderSceneError("Render did not complete.")

        produced = sorted(glob.glob(os.path.join(capture_dir, "capture*")))
        if not produced:
            raise BlenderSceneError("The render produced no image file.")
        shutil.move(produced[0], target_path)
    finally:
        shutil.rmtree(capture_dir, ignore_errors=True)

    return target_path


def capture_viewport_video(output_path, start_frame=None, end_frame=None):
    """Capture the playback range (or an explicit frame range) of the
    active viewport as an H.264 .mp4 at `output_path`.

    Blender's own render pipeline links ffmpeg directly, so this writes
    the video in one step — no PNG-sequence-then-external-ffmpeg-subprocess
    two-step is needed here, unlike the Houdini/Maya ports.

    `view_context=True`, with the captured viewport forced into true
    Camera View for the duration (see `_temporary_camera_view` and the
    matching comment on `capture_viewport`) — keeps the viewport's own
    live shading while still framing exactly through the scene's actual
    camera.

    'Rendered' shading on an add-on engine is refused outright rather than
    captured or force-rendered (see below).
    """
    scene = bpy.context.scene
    output_dir = os.path.dirname(output_path) or "."
    os.makedirs(output_dir, exist_ok=True)

    start_frame = scene.frame_start if start_frame is None else start_frame
    end_frame = scene.frame_end if end_frame is None else end_frame

    override, region_3d = _find_view3d_context()
    if override is None:
        raise BlenderSceneError(
            "No 3D Viewport is visible — make sure a Viewport is open "
            "before capturing."
        )

    # Like capture_viewport: render into a private temp dir with a fixed
    # base name, then find whatever file Blender actually produced and move
    # it to the exact requested path. Predicting Blender's own output
    # filename for FFmpeg video (base path + guessed extension) turned out
    # not to reliably match what actually gets written — safer to render
    # somewhere private and pick up whatever's there afterward, exactly the
    # same reasoning already applied to the single-frame still capture.
    space = override["area"].spaces.active
    if _needs_full_render(scene, space):
        # Deliberately NOT falling back to a full render the way the
        # single-frame capture does: rendering a whole playback range with an
        # add-on engine can take hours and would freeze Blender for all of
        # it. Better to say so than to either hang or hand back a video of
        # unshaded frames.
        raise BlenderSceneError(
            f"The viewport is set to Rendered shading with the "
            f"{scene.render.engine} engine — Blender cannot capture that "
            "from the viewport, so every frame would come out unshaded. "
            "Switch the viewport to Material Preview to capture, or render "
            "the sequence yourself and publish the file."
        )

    capture_dir = tempfile.mkdtemp(prefix="kitsu_capture_video_")
    try:
        with _temporary_render_settings(scene), _temporary_camera_view(
            region_3d, space
        ), _temporary_clean_viewport(space):
            scene.render.filepath = os.path.join(capture_dir, "capture")
            image_settings = scene.render.image_settings
            if hasattr(image_settings, "media_type"):
                # Blender 5.x: file_format now controls container/codec
                # together with media_type='VIDEO' selecting the video
                # pipeline.
                image_settings.media_type = "VIDEO"
                image_settings.file_format = "FFMPEG"
            else:
                # Older Blender: a single enum on image_settings selects
                # FFmpeg output directly.
                image_settings.file_format = "FFMPEG"
            if hasattr(scene.render, "ffmpeg"):
                scene.render.ffmpeg.format = "MPEG4"
                scene.render.ffmpeg.codec = "H264"
                scene.render.ffmpeg.constant_rate_factor = "MEDIUM"

            original_start, original_end = scene.frame_start, scene.frame_end
            scene.frame_start, scene.frame_end = start_frame, end_frame
            try:
                with bpy.context.temp_override(**override):
                    result = bpy.ops.render.opengl(
                        animation=True, view_context=True, render_keyed_only=False
                    )
            finally:
                scene.frame_start, scene.frame_end = original_start, original_end

            if result != {"FINISHED"}:
                raise BlenderSceneError("Viewport video capture did not complete.")

        produced = sorted(glob.glob(os.path.join(capture_dir, "capture*")))
        if not produced:
            raise BlenderSceneError(
                "Viewport video capture produced no output file — make sure "
                "a 3D Viewport is visible and the playback range is valid."
            )
        shutil.move(produced[0], output_path)
    finally:
        shutil.rmtree(capture_dir, ignore_errors=True)

    return output_path


def _active_camera(context=None):
    context = context or bpy.context
    camera = context.scene.camera
    if camera is None or camera.type != "CAMERA":
        return None
    return camera


def _aspect_ratio_mismatch_warning(image_width, image_height, label):
    """Compare an image's aspect ratio against the scene's current render
    resolution and return a warning string if they meaningfully differ
    (more than a rounding-level tolerance), else None.

    This exists to turn "the overlay doesn't line up with the 3D content"
    from a guess into a checkable fact: if the two aspect ratios don't
    match, no amount of FIT/STRETCH tweaking fixes it — Blender's camera
    frames a different portion of the 3D scene at a different render
    aspect ratio (via sensor-fit), even with the exact same camera
    transform, so a background image composited for one aspect ratio can
    never align with the live 3D content rendered at a different one.
    """
    render_width, render_height = render_resolution()
    if not render_width or not render_height or not image_width or not image_height:
        return None
    image_ratio = image_width / image_height
    render_ratio = render_width / render_height
    if abs(image_ratio - render_ratio) / render_ratio > 0.01:  # >1% relative difference
        return (
            f"{label} is {image_width}x{image_height} (aspect "
            f"{image_ratio:.3f}) but the scene's current render "
            f"resolution is {render_width}x{render_height} (aspect "
            f"{render_ratio:.3f}) — these don't match, so the overlay "
            "cannot align with the 3D content no matter how it's fitted "
            "to the camera frame. Check whether the render resolution "
            "was different when this was captured/published."
        )
    return None


def set_feedback_camera_background(image_path, revision):
    """Show `image_path` as the active camera's background image.

    A camera only ever has one "Kitsu feedback" background image tracked
    here — replacing it on every call, rather than stacking entries,
    mirrors the Houdini port's "a viewport only ever has one background
    image" simplification. Only visible when actually looking through
    this camera (Numpad 0), matching how a Maya image plane behaves too.

    Returns (bg, warning) — `warning` is a diagnostic string (see
    _aspect_ratio_mismatch_warning) if the image's aspect ratio doesn't
    match the scene's current render resolution, else None.
    """
    camera = _active_camera()
    if camera is None:
        raise BlenderSceneError(
            "No active camera in this scene — set one as the active "
            "camera before showing a drawover as its background image."
        )

    cam_data = camera.data
    _clear_feedback_background_images(cam_data)

    image = bpy.data.images.load(image_path, check_existing=False)
    image.name = f"Kitsu_Feedback_r{revision}"
    warning = _aspect_ratio_mismatch_warning(image.size[0], image.size[1], "This revision's image")
    bg = cam_data.background_images.new()
    bg.image = image
    bg.alpha = 1.0
    bg.display_depth = "FRONT"
    # 'STRETCH' (not Blender's default 'FIT') forces the image to fill the
    # camera's view bounds exactly, regardless of its own resolution/aspect
    # ratio -- the drawover was composited to align with the render frame
    # itself, so it must map onto that frame 1:1, not be letterboxed by
    # Blender if the served preview's actual pixel dimensions differ even
    # slightly from the render resolution (e.g. from Kitsu's own preview
    # generation/compression) -- 'FIT' would silently introduce exactly
    # that kind of small positional offset. NOTE: if the aspect ratios
    # genuinely differ (see `warning` above), STRETCH will visibly distort
    # the image rather than just offset it — that distortion is a symptom
    # of the underlying mismatch, not something this setting can fix.
    bg.frame_method = "STRETCH"
    cam_data.show_background_images = True
    return bg, warning


_FEEDBACK_PREFIX = "Kitsu_Feedback_"


def _clear_feedback_background_images(cam_data):
    """Remove only the background image entries this addon itself created
    (tracked by name prefix) — never anything else the user may have set
    up, even if it happens to have no `.image` (e.g. a MOVIE_CLIP-sourced
    entry, where `.image` is always None)."""
    for bg in list(cam_data.background_images):
        is_ours = (
            bg.source == "IMAGE"
            and bg.image is not None
            and bg.image.name.startswith(_FEEDBACK_PREFIX)
        ) or (
            bg.source == "MOVIE_CLIP"
            and bg.clip is not None
            and bg.clip.name.startswith(_FEEDBACK_PREFIX)
        )
        if not is_ours:
            continue
        image, clip = bg.image, bg.clip
        cam_data.background_images.remove(bg)
        if image is not None:
            bpy.data.images.remove(image)
        if clip is not None:
            bpy.data.movieclips.remove(clip)


def set_feedback_drawover_sequence(directory, file_pattern, first_frame, frame_count, revision):
    """Show a per-frame drawover sequence as the active camera's background
    image, driven by a `MovieClip` datablock configured as a numbered
    image sequence.

    `directory`/`file_pattern` (a "name.####.png"-style Blender sequence
    name) point at files written by the caller: each frame in the covered
    range has that frame's drawover composited on a transparent
    background, or a fully transparent image for unannotated frames — so
    scrubbing the timeline shows markup only on the exact frames the
    reviewer drew on. Blender's clip system re-reads the frame-matched
    file on its own; no custom per-frame expression/driver is needed here
    (unlike the Maya port).
    """
    camera = _active_camera()
    if camera is None:
        raise BlenderSceneError(
            "No active camera in this scene — set one as the active "
            "camera before showing drawovers as its background."
        )

    cam_data = camera.data
    _clear_feedback_background_images(cam_data)

    clip_name = f"Kitsu_Feedback_r{revision}"
    existing_clip = bpy.data.movieclips.get(clip_name)
    if existing_clip is not None:
        bpy.data.movieclips.remove(existing_clip)

    first_file_path = os.path.join(directory, file_pattern % first_frame)
    clip = bpy.data.movieclips.load(first_file_path)
    clip.name = clip_name
    clip.frame_start = first_frame
    # frame_duration is read-only -- Blender auto-detects it from however
    # many numbered files it finds in `directory` matching the sequence's
    # naming pattern, which is exactly `frame_count` here since
    # KITSU_OT_show_video_drawovers writes one contiguous file per frame
    # into a fresh, revision-specific directory (never a stray leftover
    # file to throw the count off).
    if clip.frame_duration != frame_count:
        raise BlenderSceneError(
            f"Blender detected {clip.frame_duration} frame(s) in the "
            f"drawover sequence at {directory!r}, expected {frame_count} — "
            "the sequence files may be missing or misnamed."
        )

    bg = cam_data.background_images.new()
    bg.source = "MOVIE_CLIP"
    bg.clip = clip
    bg.clip_user.use_render_undistorted = False
    bg.alpha = 1.0
    bg.display_depth = "FRONT"
    # See the matching comment in set_feedback_camera_background: 'STRETCH'
    # forces exact frame-fill instead of Blender's default 'FIT', so the
    # per-frame overlay aligns with the render frame 1:1 rather than being
    # letterboxed by any small resolution/aspect-ratio mismatch.
    bg.frame_method = "STRETCH"
    cam_data.show_background_images = True
    return bg


def set_timeline_markers(prefix, frame_labels):
    """Create one timeline marker per annotated frame.

    `frame_labels` maps a Blender frame number to the marker's label. Any
    existing marker whose name starts with `prefix` is removed first, so
    re-clicking for the same revision replaces its markers instead of
    stacking duplicates (same contract as the feedback background image).

    `bpy.types.TimelineMarker` has no color property (confirmed against
    the current Blender API) — the name-encoded label is the only
    per-marker metadata available, same information Houdini's colored
    bookmarks show, just without color banding.
    """
    scene = bpy.context.scene
    markers = scene.timeline_markers

    for marker in [m for m in markers if m.name.startswith(prefix)]:
        markers.remove(marker)

    created = []
    for frame, label in sorted(frame_labels.items()):
        marker = markers.new(name=label, frame=frame)
        created.append(marker)
    return created

"""Renders Kitsu drawover annotations (Fabric.js shapes) onto an image file.

Kitsu stores drawover markup as a small Fabric.js canvas per preview file
revision: `preview["annotations"]` is a list of entries shaped like
`{"time": 0, "frame": 0, "drawing": {"objects": [...]}}`. For a still
image (not a video), the relevant entry is the one at time == 0 — Kitsu's
own player picks the same entry the same way. Each object in
`drawing.objects` is a Fabric.js shape dict: a `type` plus position/size/
color fields, with a few type-specific extras (radius for circles,
x1/y1/x2/y2 for lines/arrows, path commands for freehand strokes, text
fields for text objects).

IMPORTANT: object coordinates are NOT authored against the image's native
pixel size. Each object carries its own `canvasWidth`/`canvasHeight` — the
on-screen size of Kitsu's drawing canvas *at the moment it was drawn* (a
browser player widget, commonly much smaller than the actual image
resolution). Kitsu's own renderer maps that reference frame onto whatever
target it's rendering into with a "contain" fit (like CSS
`object-fit: contain`): scale by `min(target_w/ref_w, target_h/ref_h)` and
center the result. This module replicates that mapping, once per object
(falling back to the preview's own width/height only if an object doesn't
carry its own canvasWidth/canvasHeight, matching Kitsu's own fallback).

This is the Blender port's replacement for the Maya/Houdini/Krita ports'
`annotations.py`, which drew directly onto a `QPixmap` with `QPainter`.
Blender has no bundled Qt, so this is a from-scratch reimplementation, not
a port: shapes (rect/circle/ellipse/line/arrow/path/the pressure-sensitive
"PSStroke" freehand brush) are rasterized with plain `numpy` array math —
a supersampled fill/stroke rasterizer, since numpy has no drawing
primitives of its own — and only *text* objects use Blender's own `blf` +
`gpu` (rendered into a `GPUOffScreen` and composited back in), since text
shaping/glyph rendering has no numpy equivalent. The numpy path works
without any GPU context at all (so it's callable from a background
thread's *result handling* on the main thread just as easily as from a
button click), which is why it — not `gpu` — carries the shape work; only
the text pass needs a live GL context and so must run on the main thread.

Known simplifications, matching (not exceeding) the other ports' own
documented limitations: rotation is not replicated for freehand strokes,
plain lines, or arrows (rare in practice); the eraser tool's mask and
nested groups are not supported; stroke width scaling under non-uniform
scaleX/scaleY approximates Qt's cosmetic-pen transform behavior with a
single averaged scale factor rather than true anisotropic stroking; line
joins/caps are approximate (extended-rectangle "square-ish" caps) rather
than Qt's exact round caps/joins. A shape that fails to draw is skipped,
never breaking the rest of the drawover — same contract as the other
ports.
"""

import math
import re
import shutil

import mathutils

import numpy as np

import bpy

_RGBA_RE = re.compile(
    r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)", re.IGNORECASE
)

_SUPERSAMPLE = 3


def picture_annotation_objects(preview):
    """Return the Fabric shape dicts for a still-image preview's drawover."""
    annotations = preview.get("annotations") or []
    if not annotations:
        return []
    entry = next((a for a in annotations if a.get("time") == 0), annotations[0])
    return (entry.get("drawing") or {}).get("objects") or []


def render_annotations_to_file(base_image_path, objects, output_path, fallback_width=None, fallback_height=None):
    """Composite `objects` on top of the image at `base_image_path`, writing
    the result to `output_path`. If `objects` is empty, `output_path` is
    just a copy of the input — callers can always use `output_path`
    afterward regardless of whether there was anything to draw. Returns
    `output_path`.
    """
    if not objects:
        shutil.copyfile(base_image_path, output_path)
        return output_path

    source = bpy.data.images.load(base_image_path, check_existing=False)
    try:
        width, height = source.size[0], source.size[1]
        if width <= 0 or height <= 0:
            shutil.copyfile(base_image_path, output_path)
            return output_path

        buffer = np.array(source.pixels[:], dtype=np.float32).reshape(height, width, 4)
    finally:
        bpy.data.images.remove(source)

    # Blender image pixel data is bottom-left origin; flip to top-left
    # (Fabric.js/the other ports' Qt convention) for the duration of
    # compositing, then flip back before writing out.
    buffer = np.flipud(buffer).copy()

    target_w, target_h = width * _SUPERSAMPLE, height * _SUPERSAMPLE
    work = _nearest_upsample(buffer, _SUPERSAMPLE)

    for obj in objects:
        try:
            mapping = _contain_mapping(
                target_w,
                target_h,
                obj.get("canvasWidth") or fallback_width or width,
                obj.get("canvasHeight") or fallback_height or height,
            )
            _draw_object(work, obj, mapping)
        except Exception:
            continue

    result = _box_downsample(work, _SUPERSAMPLE)
    result = np.clip(result, 0.0, 1.0)
    result = np.flipud(result)  # back to bottom-left origin for bpy.data.images

    _write_image(result, width, height, output_path)
    return output_path


def render_video_frame_to_file(objects, output_path, canvas_width, canvas_height):
    """Render one frame of a per-frame drawover *overlay* sequence (objects
    come from video_annotations.annotated_frames, keyed by frame) — always
    onto a fully transparent canvas, whether or not this particular frame
    has any objects to draw. This is deliberately never composited onto
    the movie's own frame image: per the Houdini port's documented
    behavior, the sequence is shown as a background layered over the live
    viewport, so unannotated frames must be blank (nothing to hide the
    viewport behind) and annotated frames must show only the markup
    (nothing else painted over the viewport either)."""
    if not objects:
        _write_transparent(canvas_width, canvas_height, output_path)
        return output_path

    target_w, target_h = canvas_width * _SUPERSAMPLE, canvas_height * _SUPERSAMPLE
    work = np.zeros((target_h, target_w, 4), dtype=np.float32)
    for obj in objects:
        try:
            mapping = _contain_mapping(
                target_w,
                target_h,
                obj.get("canvasWidth") or canvas_width,
                obj.get("canvasHeight") or canvas_height,
            )
            _draw_object(work, obj, mapping)
        except Exception:
            continue
    result = np.clip(_box_downsample(work, _SUPERSAMPLE), 0.0, 1.0)
    result = np.flipud(result)
    _write_image(result, canvas_width, canvas_height, output_path)
    return output_path


def _write_transparent(width, height, output_path):
    buffer = np.zeros((height, width, 4), dtype=np.float32)
    _write_image(buffer, width, height, output_path)


def _write_image(buffer, width, height, output_path):
    out_image = bpy.data.images.new("kitsu_annotation_tmp", width, height, alpha=True)
    try:
        out_image.pixels.foreach_set(buffer.astype(np.float32).ravel())
        out_image.filepath_raw = output_path
        out_image.file_format = "PNG"
        out_image.save()
    finally:
        bpy.data.images.remove(out_image)


def _nearest_upsample(buffer, factor):
    return np.repeat(np.repeat(buffer, factor, axis=0), factor, axis=1)


def _box_downsample(buffer, factor):
    h, w = buffer.shape[0] // factor, buffer.shape[1] // factor
    reshaped = buffer[: h * factor, : w * factor].reshape(h, factor, w, factor, 4)
    return reshaped.mean(axis=(1, 3))


def _contain_mapping(target_width, target_height, ref_width, ref_height):
    """Confirmed verbatim against Kitsu's actual `getAnnotationContainMapping`
    (src/lib/players/annotation.js): fit ref_w x ref_h into
    target_w x target_h, centered, uniformly scaled — including its
    width-only fallback for the (presumably rare) case of an object with a
    recorded canvasWidth but no canvasHeight."""
    if ref_width and ref_height:
        scale = min(target_width / ref_width, target_height / ref_height)
        offset_x = (target_width - ref_width * scale) / 2
        offset_y = (target_height - ref_height * scale) / 2
        return scale, offset_x, offset_y
    if ref_width:
        return target_width / ref_width, 0.0, 0.0
    return 1.0, 0.0, 0.0


def _color(value):
    if not value or value == "transparent":
        return None
    match = _RGBA_RE.match(value.strip()) if isinstance(value, str) else None
    if match:
        r, g, b, a = match.groups()
        return (int(r) / 255.0, int(g) / 255.0, int(b) / 255.0, float(a) if a is not None else 1.0)
    return _parse_css_color(value)


def _parse_css_color(value):
    value = str(value).strip()
    if value.startswith("#"):
        hex_value = value[1:]
        try:
            if len(hex_value) == 3:
                r, g, b = (int(c * 2, 16) for c in hex_value)
                return (r / 255.0, g / 255.0, b / 255.0, 1.0)
            if len(hex_value) == 6:
                r, g, b = (int(hex_value[i : i + 2], 16) for i in (0, 2, 4))
                return (r / 255.0, g / 255.0, b / 255.0, 1.0)
            if len(hex_value) == 8:
                r, g, b, a = (int(hex_value[i : i + 2], 16) for i in (0, 2, 4, 6))
                return (r / 255.0, g / 255.0, b / 255.0, a / 255.0)
        except ValueError:
            return None
    named = _NAMED_COLORS.get(value.lower())
    if named:
        return named
    return None


_NAMED_COLORS = {
    "black": (0.0, 0.0, 0.0, 1.0),
    "white": (1.0, 1.0, 1.0, 1.0),
    "red": (1.0, 0.0, 0.0, 1.0),
    "green": (0.0, 0.5019607843137255, 0.0, 1.0),
    "blue": (0.0, 0.0, 1.0, 1.0),
    "yellow": (1.0, 1.0, 0.0, 1.0),
    "orange": (1.0, 0.6470588235294118, 0.0, 1.0),
}


def _blend_pixel(buffer, y, x, color):
    r, g, b, a = color
    if a <= 0:
        return
    inv = 1.0 - a
    buffer[y, x, 0] = buffer[y, x, 0] * inv + r * a
    buffer[y, x, 1] = buffer[y, x, 1] * inv + g * a
    buffer[y, x, 2] = buffer[y, x, 2] * inv + b * a
    buffer[y, x, 3] = buffer[y, x, 3] * inv + a


def _fill_polygon(buffer, points, color):
    """Even-odd scanline fill of a (possibly concave, non-self-intersecting)
    polygon, vectorized over its bounding box with numpy."""
    if color is None or len(points) < 3:
        return
    h, w = buffer.shape[:2]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    min_x = max(int(math.floor(min(xs))), 0)
    max_x = min(int(math.ceil(max(xs))), w - 1)
    min_y = max(int(math.floor(min(ys))), 0)
    max_y = min(int(math.ceil(max(ys))), h - 1)
    if max_x < min_x or max_y < min_y:
        return

    yy, xx = np.mgrid[min_y : max_y + 1, min_x : max_x + 1]
    px = xx.astype(np.float64) + 0.5
    py = yy.astype(np.float64) + 0.5

    n = len(points)
    inside = np.zeros(px.shape, dtype=bool)
    xj, yj = points[-1]
    for i in range(n):
        xi, yi = points[i]
        denom = (yj - yi) if (yj - yi) != 0 else 1e-9
        crosses = (yi > py) != (yj > py)
        x_intersect = (xj - xi) * (py - yi) / denom + xi
        inside ^= crosses & (px < x_intersect)
        xj, yj = xi, yi

    if not inside.any():
        return

    region = buffer[min_y : max_y + 1, min_x : max_x + 1]
    r, g, b, a = color
    for channel, value in enumerate((r, g, b)):
        region[..., channel] = np.where(inside, region[..., channel] * (1 - a) + value * a, region[..., channel])
    region[..., 3] = np.where(inside, region[..., 3] * (1 - a) + a, region[..., 3])


def _stroke_polyline(buffer, points, color, half_width, closed=False):
    if color is None or half_width <= 0 or len(points) < 2:
        return
    segments = list(zip(points, points[1:]))
    if closed:
        segments.append((points[-1], points[0]))
    for (x1, y1), (x2, y2) in segments:
        _fill_polygon(buffer, _thick_segment_quad(x1, y1, x2, y2, half_width), color)


def _thick_segment_quad(x1, y1, x2, y2, half_width):
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-6:
        # Degenerate segment (a single point): draw a small square so it's
        # still visible rather than silently vanishing.
        return [
            (x1 - half_width, y1 - half_width),
            (x1 + half_width, y1 - half_width),
            (x1 + half_width, y1 + half_width),
            (x1 - half_width, y1 + half_width),
        ]
    ux, uy = dx / length, dy / length
    px, py = -uy * half_width, ux * half_width
    # Extend along the segment direction by half_width for a "square-ish"
    # cap approximation (Qt's RoundCap is not replicated exactly).
    ex, ey = ux * half_width, uy * half_width
    return [
        (x1 - ex + px, y1 - ey + py),
        (x2 + ex + px, y2 + ey + py),
        (x2 + ex - px, y2 + ey - py),
        (x1 - ex - px, y1 - ey - py),
    ]


def _object_transform(obj, mapping):
    scale, offset_x, offset_y = mapping
    scale_x = (obj.get("scaleX") or 1) * scale
    scale_y = (obj.get("scaleY") or 1) * scale
    left = (obj.get("left") or 0) * scale + offset_x
    top = (obj.get("top") or 0) * scale + offset_y
    angle = math.radians(obj.get("angle") or 0)
    return scale_x, scale_y, left, top, angle


def _transform_point(local_x, local_y, width, height, transform):
    scale_x, scale_y, left, top, angle = transform
    cx, cy = local_x - width / 2, local_y - height / 2
    sx, sy = cx * scale_x, cy * scale_y
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    rx = sx * cos_a - sy * sin_a
    ry = sx * sin_a + sy * cos_a
    return (
        rx + left + (width * scale_x) / 2,
        ry + top + (height * scale_y) / 2,
    )


def _average_scale(transform):
    scale_x, scale_y = transform[0], transform[1]
    return (abs(scale_x) + abs(scale_y)) / 2 or 1.0


def _draw_object(buffer, obj, mapping):
    obj_type = obj.get("type")
    # Note: PSStroke is deliberately NOT lowercased/normalized by Kitsu —
    # match the exact mixed-case string.
    if obj_type == "PSStroke":
        _draw_ps_stroke(buffer, obj, mapping)
    elif obj_type in ("line", "arrow"):
        _draw_line(buffer, obj, mapping, arrow=(obj_type == "arrow"))
    elif obj_type in ("i-text", "text", "textbox"):
        _draw_text(buffer, obj, mapping)
    elif obj_type == "path":
        _draw_path(buffer, obj, mapping)
    elif obj_type in ("rect", "circle", "ellipse"):
        _draw_basic_shape(buffer, obj, mapping)
    # 'group', 'eraser', and anything else: not supported yet, skip.


def _basic_shape_size(obj):
    obj_type = obj.get("type")
    if obj_type == "circle":
        radius = obj.get("radius") or 0
        return radius * 2, radius * 2
    if obj_type == "ellipse":
        return (obj.get("rx") or 0) * 2, (obj.get("ry") or 0) * 2
    return obj.get("width") or 0, obj.get("height") or 0


def _ellipse_points(width, height, segments=48):
    cx, cy = width / 2, height / 2
    rx, ry = width / 2, height / 2
    return [
        (cx + rx * math.cos(t), cy + ry * math.sin(t))
        for t in (2 * math.pi * i / segments for i in range(segments))
    ]


def _rect_points(width, height):
    return [(0, 0), (width, 0), (width, height), (0, height)]


def _draw_basic_shape(buffer, obj, mapping):
    width, height = _basic_shape_size(obj)
    if width <= 0 or height <= 0:
        return
    transform = _object_transform(obj, mapping)
    is_round = obj.get("type") in ("circle", "ellipse")
    local_points = _ellipse_points(width, height) if is_round else _rect_points(width, height)
    points = [_transform_point(x, y, width, height, transform) for x, y in local_points]

    fill = _color(obj.get("fill"))
    if fill is not None:
        _fill_polygon(buffer, points, fill)

    stroke = _color(obj.get("stroke"))
    if stroke is not None:
        stroke_width = max(obj.get("strokeWidth") or 1, 0.1) * _average_scale(transform)
        _stroke_polyline(buffer, points, stroke, stroke_width / 2, closed=True)


def _draw_path(buffer, obj, mapping):
    commands = obj.get("path") or []
    xs, ys = [], []
    for command in commands:
        for i in range(1, len(command), 2):
            xs.append(command[i])
            ys.append(command[i + 1])
    if not xs:
        return
    origin_x, origin_y = min(xs), min(ys)

    local_points = []
    current = None
    for command in commands:
        op = command[0]
        coords = [
            (command[i] - origin_x, command[i + 1] - origin_y)
            for i in range(1, len(command), 2)
        ]
        if op == "M" and coords:
            current = coords[0]
            local_points.append(current)
        elif op == "L" and coords:
            current = coords[0]
            local_points.append(current)
        elif op == "Q" and len(coords) >= 2 and current is not None:
            local_points.extend(_sample_quadratic(current, coords[0], coords[1]))
            current = coords[1]
        elif op == "C" and len(coords) >= 3 and current is not None:
            local_points.extend(_sample_cubic(current, coords[0], coords[1], coords[2]))
            current = coords[2]
        elif op == "Z":
            pass  # closing handled by `closed=True` in the stroke call below

    if not local_points:
        return

    scale, offset_x, offset_y = mapping
    scale_x = (obj.get("scaleX") or 1) * scale
    scale_y = (obj.get("scaleY") or 1) * scale
    left = (obj.get("left") or 0) * scale + offset_x
    top = (obj.get("top") or 0) * scale + offset_y
    points = [(left + x * scale_x, top + y * scale_y) for x, y in local_points]

    fill = _color(obj.get("fill"))
    if fill is not None:
        _fill_polygon(buffer, points, fill)

    stroke = _color(obj.get("stroke"))
    if stroke is not None:
        stroke_width = max(obj.get("strokeWidth") or 1, 0.1) * ((abs(scale_x) + abs(scale_y)) / 2 or 1.0)
        _stroke_polyline(buffer, points, stroke, stroke_width / 2, closed=False)


def _sample_quadratic(p0, p1, p2, segments=10):
    points = []
    for i in range(1, segments + 1):
        t = i / segments
        mt = 1 - t
        x = mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0]
        y = mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1]
        points.append((x, y))
    return points


def _sample_cubic(p0, p1, p2, p3, segments=14):
    points = []
    for i in range(1, segments + 1):
        t = i / segments
        mt = 1 - t
        x = mt**3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t**3 * p3[0]
        y = mt**3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t**3 * p3[1]
        points.append((x, y))
    return points


def _draw_ps_stroke(buffer, obj, mapping):
    """Replicate PSStroke._renderStroke from fabricjs-psbrush: a chain of
    quadratic-curve segments through consecutive point midpoints, each
    segment's width set by that point's pressure * strokeWidth.

    Confirmed against Kitsu's actual frontend source
    (src/composables/players/annotation.js): a PSStroke is repositioned
    for display the same way as every other shape --
    `psstroke.set('left', obj.left * scale + offsetX)` etc — i.e. via the
    object's own left/top/scaleX/scaleY, exactly like rect/circle/text/
    path, NOT by scaling each strokePoint directly (what this function did
    before, and incorrectly — strokePoints are LOCAL to the stroke's own
    bounding box, not already-absolute canvas coordinates)."""
    points = obj.get("strokePoints") or []
    if len(points) < 2:
        return

    xs = [p.get("x") or 0 for p in points]
    ys = [p.get("y") or 0 for p in points]
    min_x, min_y = min(xs), min(ys)
    width, height = max(xs) - min_x, max(ys) - min_y

    transform = _object_transform(obj, mapping)
    stroke_width = obj.get("strokeWidth") or 1
    stroke_color = _color(obj.get("stroke")) or (0.0, 0.0, 0.0, 1.0)
    width_scale = _average_scale(transform)

    def mapped(point):
        local_x = (point.get("x") or 0) - min_x
        local_y = (point.get("y") or 0) - min_y
        x, y = _transform_point(local_x, local_y, width, height, transform)
        pressure = point.get("pressure")
        return x, y, (pressure if pressure is not None else 1)

    mapped_points = [mapped(p) for p in points]
    count = len(mapped_points)
    p1 = mapped_points[0]
    p2 = mapped_points[1]
    mid = p1
    for i in range(1, count):
        next_mid = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
        segment = _sample_quadratic((mid[0], mid[1]), (p1[0], p1[1]), next_mid, segments=6)
        polyline = [(mid[0], mid[1])] + segment
        half_width = max(p1[2] * stroke_width * width_scale, 0.5) / 2
        _stroke_polyline(buffer, polyline, stroke_color, half_width, closed=False)

        mid = next_mid
        p1 = mapped_points[i]
        p2 = mapped_points[i + 1] if i + 1 < count else p1


def _draw_line(buffer, obj, mapping, arrow):
    """Confirmed against Kitsu's actual frontend source
    (src/lib/players/arrowshape.js): Fabric's Line/Arrow stores x1/y1/x2/y2
    as its native geometry but is repositioned for display via the exact
    same generic left/top/scaleX/scaleY transform as every other shape
    (there is no line/arrow-specific case in the composable that applies
    the contain-fit mapping — it uses the shared "base" object transform).
    This function used to scale x1/y1/x2/y2 directly and ignore the
    object's own left/top/scaleX/scaleY entirely, which was wrong;
    endpoints are now treated as LOCAL coordinates within the line's own
    bounding box and transformed via _transform_point, exactly like
    rect/circle/text/path already are.
    """
    x1, y1 = obj.get("x1") or 0, obj.get("y1") or 0
    x2, y2 = obj.get("x2") or 0, obj.get("y2") or 0
    min_x, min_y = min(x1, x2), min(y1, y2)
    width, height = abs(x2 - x1), abs(y2 - y1)

    transform = _object_transform(obj, mapping)
    p1 = _transform_point(x1 - min_x, y1 - min_y, width, height, transform)
    p2 = _transform_point(x2 - min_x, y2 - min_y, width, height, transform)
    width_scale = _average_scale(transform)

    stroke_width = max(obj.get("strokeWidth") or 1, 0.1) * width_scale
    color = _color(obj.get("stroke")) or (0.0, 0.0, 0.0, 1.0)
    _stroke_polyline(buffer, [p1, p2], color, stroke_width / 2, closed=False)

    if not arrow:
        return

    head_size = (obj.get("arrowHeadSize") or 15) * width_scale
    head_width = (obj.get("arrowHeadWidth") or 12) * width_scale
    x1t, y1t = p1
    x2t, y2t = p2
    angle = math.atan2(y2t - y1t, x2t - x1t)
    spread = math.atan2(head_width / 2, head_size) if head_size else 0

    left_x = x2t - head_size * math.cos(angle - spread)
    left_y = y2t - head_size * math.sin(angle - spread)
    right_x = x2t - head_size * math.cos(angle + spread)
    right_y = y2t - head_size * math.sin(angle + spread)

    _fill_polygon(buffer, [(x2t, y2t), (left_x, left_y), (right_x, right_y)], color)


def _draw_text(buffer, obj, mapping):
    """Text is the one shape type that needs Blender's own text rendering
    (`blf`) since numpy has no font/glyph support — rendered into a
    temporary GPUOffScreen sized to the working (supersampled) buffer, then
    alpha-composited in. Must run with a live GL context, i.e. on the main
    thread (see the module docstring).

    Note this means text objects cannot render under `blender --background`
    (Blender raises "GPU functions for drawing are not available in
    background mode" the moment a GPUOffScreen is created there) — caught
    by _draw_object's caller like any other per-shape failure, so a text
    annotation is simply skipped rather than breaking the rest of the
    drawover. This addon only ever runs inside a normal, windowed Blender
    session (it is a UI panel/operator addon, never invoked via
    --background), where a GPU context always exists, so this is not a
    real-world limitation — only something to be aware of if this module
    is ever exercised from a background/CI script directly.
    """
    import blf
    import gpu

    text = obj.get("text") or ""
    if not text:
        return

    width, height = _basic_shape_size(obj)
    transform = _object_transform(obj, mapping)
    font_size = max(obj.get("fontSize") or 16, 1) * _average_scale(transform)
    wrap = obj.get("type") == "textbox"

    font_id = 0
    blf.size(font_id, int(font_size))
    if not wrap:
        text_width, text_height = blf.dimensions(font_id, text)
        width = max(width, text_width)
        height = max(height, text_height * 1.3)

    lines = _wrap_text(font_id, text, width) if wrap else [text]
    line_height = font_size * 1.3

    buf_h, buf_w = buffer.shape[:2]
    offscreen = gpu.types.GPUOffScreen(buf_w, buf_h)
    text_color = _color(obj.get("fill")) or (0.0, 0.0, 0.0, 1.0)
    background = _color(obj.get("backgroundColor"))

    with offscreen.bind():
        fb = gpu.state.active_framebuffer_get()
        fb.clear(color=(0.0, 0.0, 0.0, 0.0))
        # A bare GPUOffScreen has no projection matrix of its own -- without
        # explicitly establishing one, blf.position()/blf.draw() (and our own
        # polygon shader) draw using whatever matrix happened to be left on
        # the stack, which in a background/headless run is not a pixel-space
        # mapping onto this offscreen at all, so nothing ends up visible.
        with gpu.matrix.push_pop():
            gpu.matrix.load_matrix(mathutils.Matrix.Identity(4))
            gpu.matrix.load_projection_matrix(_pixel_projection_matrix(buf_w, buf_h))

            if background is not None:
                bg_points = [
                    _transform_point(x, y, width, height, transform)
                    for x, y in _rect_points(width, height)
                ]
                _draw_gpu_polygon(bg_points, background, buf_w, buf_h)

            blf.color(font_id, *text_color)
            for i, line in enumerate(lines):
                local_y = height - (i + 1) * line_height
                origin = _transform_point(0, local_y, width, height, transform)
                blf.position(font_id, origin[0], buf_h - origin[1] - line_height, 0)
                blf.draw(font_id, line)

        buffer_data = fb.read_color(0, 0, buf_w, buf_h, 4, 0, "FLOAT")
        text_layer = np.array(buffer_data, dtype=np.float32).reshape(buf_h, buf_w, 4)
    offscreen.free()

    text_layer = np.flipud(text_layer)  # blf/gpu use bottom-left origin; our buffer is top-left
    alpha = text_layer[..., 3:4]
    buffer[..., :3] = buffer[..., :3] * (1 - alpha) + text_layer[..., :3] * alpha
    buffer[..., 3:4] = buffer[..., 3:4] * (1 - alpha) + alpha


def _pixel_projection_matrix(width, height):
    """An orthographic projection mapping pixel coordinates (0,0)-(width,
    height), bottom-left origin, directly onto clip space -- the matrix a
    GPUOffScreen needs explicitly set up before blf/gpu drawing lands on
    the pixels callers expect (see the comment in _draw_text)."""
    return mathutils.Matrix(
        (
            (2.0 / width, 0.0, 0.0, -1.0),
            (0.0, 2.0 / height, 0.0, -1.0),
            (0.0, 0.0, -1.0, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        )
    )


def _draw_gpu_polygon(points, color, buf_w, buf_h):
    import gpu
    from gpu_extras.batch import batch_for_shader

    # Fan-triangulate (points are a simple convex/near-convex quad here).
    indices = [(0, i, i + 1) for i in range(1, len(points) - 1)]
    coords = [(x, buf_h - y) for x, y in points]  # flip to gpu's bottom-left origin
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "TRIS", {"pos": coords}, indices=indices)
    shader.bind()
    shader.uniform_float("color", color)
    gpu.state.blend_set("ALPHA")
    batch.draw(shader)
    gpu.state.blend_set("NONE")


def _wrap_text(font_id, text, max_width):
    import blf

    words = text.split(" ")
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        width, _ = blf.dimensions(font_id, candidate)
        if width > max_width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [text]

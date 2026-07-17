"""Extract per-frame drawover annotations from a Kitsu video preview.

For still images Kitsu stores a single annotation entry at time == 0
(handled by annotations_gpu.py); for movie previews it stores one entry per
annotated frame, keyed by playback time in seconds:

    {"time": 3.2, "drawing": {"objects": [...]}}

Kitsu's own player converts between time and frame with the production's
fps (frame = round(time * fps)), so the same conversion is applied here.
Newer Zou versions also record the frame number directly on the entry;
when present it is trusted over the time-based computation, EXCEPT that
it is treated here as 1-based (the first frame is reported as 1, not 0)
and normalized down by one to match the time-based path's own 0-based
result. This is a hypothesis, not a confirmed Zou behavior (there is no
public schema for this field to check it against) — raised because a
real Kitsu server produced markers/drawovers that landed exactly one
frame later than Kitsu's own display, consistently (not an occasional
rounding artifact, which is what pointed at an indexing-base mismatch
specifically rather than the time*fps rounding itself). If this
normalization turns out to be wrong (e.g. it shifts the error to "one
frame earlier" instead of fixing it), the "frame" field is not 1-based
and something else is going on — revert this specific `- 1` and look
elsewhere (e.g. whether the published video's actual first encoded frame
truly lines up with scene.frame_start at publish time).

Frames are returned 0-based, exactly as Kitsu counts them internally —
mapping them onto Blender's own timeline (offsetting by scene.frame_start,
since frame 0 of a published capture is the first frame of the playback
range) is the caller's job.
"""


def annotated_frames(preview, fps):
    """Return {frame (int, 0-based): [fabric shape dicts]} for a video.

    Entries without any drawn objects are skipped, so every returned frame
    actually has visible markup.
    """
    result = {}
    if not fps or fps <= 0:
        return result

    for entry in preview.get("annotations") or []:
        objects = (entry.get("drawing") or {}).get("objects") or []
        if not objects:
            continue

        frame = entry.get("frame")
        if frame is not None:
            frame = frame - 1  # Zou's "frame" field is 1-based; normalize to 0-based
        else:
            frame = round(float(entry.get("time") or 0) * fps)
        result.setdefault(int(frame), []).extend(objects)

    return result

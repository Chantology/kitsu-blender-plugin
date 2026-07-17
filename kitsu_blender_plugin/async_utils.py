"""Non-blocking network calls from inside Blender's UI thread.

Confirmed against Blender's own "Python Threads are Not Supported" docs: a
background `threading.Thread` may freely do gazu/HTTP work as long as
nothing — including the main thread — touches `bpy` while that thread is
still running. Only once the thread has finished is it safe to touch `bpy`
again. The sanctioned pattern is a background thread that does pure
networking, pushing its result into a plain queue, drained by a
`bpy.app.timers.register` callback running on the main thread — this
module provides that pattern once, so every network-calling operator
(login, detect task, load comments, publish, ...) doesn't reimplement it.
"""

import queue
import threading

import bpy

_POLL_INTERVAL = 0.05


def run_async(work_fn, on_done, on_error=None):
    """Run `work_fn()` in a background thread. `work_fn` must not touch
    `bpy` in any way — it should only do network/file I/O and return a
    plain result.

    Once the thread finishes, `on_done(result)` (or `on_error(exception)`
    on failure) is called on Blender's main thread via a timer, so it's
    safe for those callbacks to update `state.runtime` and trigger a
    redraw.
    """
    result_queue = queue.Queue()

    def _worker():
        try:
            result = work_fn()
        except Exception as exc:  # noqa: BLE001 - forwarded to on_error, not swallowed
            result_queue.put(("error", exc))
        else:
            result_queue.put(("ok", result))

    def _poll():
        try:
            status, payload = result_queue.get_nowait()
        except queue.Empty:
            return _POLL_INTERVAL
        if status == "ok":
            on_done(payload)
        elif on_error is not None:
            on_error(payload)
        else:
            raise payload
        return None  # one-shot: stop polling once the result has been handled

    threading.Thread(target=_worker, daemon=True).start()
    bpy.app.timers.register(_poll, first_interval=_POLL_INTERVAL)


# callback -> the actual wrapper function registered with bpy.app.timers.
# bpy.app.timers only knows about the exact function object it was given,
# so tracking this ourselves is what makes register_periodic idempotent and
# unregister_periodic able to find the right timer to cancel.
_periodic_wrappers = {}


def register_periodic(callback, interval):
    """Register `callback` to run every `interval` seconds until it returns
    False (or raises) — used for the 60s comment auto-refresh and the 30s
    timesheet auto-advance tick. `callback` itself runs on the main thread
    (it's a normal bpy.app.timers callback), so it may touch `bpy` directly,
    but should kick off `run_async` for any actual network call rather than
    blocking here.
    """
    if callback in _periodic_wrappers:
        return  # already running

    def _tick():
        keep_going = callback()
        if not keep_going:
            _periodic_wrappers.pop(callback, None)
            return None
        return interval

    _periodic_wrappers[callback] = _tick
    bpy.app.timers.register(_tick, first_interval=interval, persistent=True)


def unregister_periodic(callback):
    tick = _periodic_wrappers.pop(callback, None)
    if tick is not None and bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)


def tag_redraw_all():
    """Force every UI area to redraw.

    Needed after `state.runtime` is updated from an `on_done`/`on_error`
    callback or a periodic timer tick: those run outside Blender's normal
    event handling, so a plain Python attribute write on `state.runtime`
    (not a bpy property) doesn't itself trigger a redraw the way editing a
    bpy.props value through the UI would.
    """
    if not bpy.context or not bpy.context.window_manager:
        return
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            area.tag_redraw()

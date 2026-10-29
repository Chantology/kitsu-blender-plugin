"""Resolve a Kitsu asset/shot task from a Blender scene's folder structure.

Two contexts are supported, matching Kitsu's own two ways of organizing
production entities:

- **Assets** (characters, props, sets, ...):
      .../<AssetType>/<AssetName>/<TaskType>/<file>.blend
  e.g. assets/characters/Hero/modeling/Hero_modeling.blend

- **Shots**, for a movie / short / commercial (no episodes):
      .../<Sequence>/<Shot>/<TaskType>/<file>.blend
  e.g. shots/seq010/sh0100/animation/sh0100_animation.blend

- **Shots**, for a TV series (with episodes — the optional extra level):
      .../<Episode>/<Sequence>/<Shot>/<TaskType>/<file>.blend
  e.g. episodes/ep01/seq010/sh0100/animation/sh0100_animation.blend

Whether a scene is an asset or a shot is decided from an anchor folder
somewhere in its path (case-insensitive): a folder named `shots`, `shot`,
`sequences`, `sequence`, `episodes`, or `episode` means shot context;
`assets` or `asset` means asset context. With no such anchor the scene is
treated as an asset.

Whether shots live under episodes is decided from the *project*, not the
folder count: only a `tvshow` production has episodes, so for a TV-show
project the extra `<Episode>` level is expected and for a movie/short it
is not. This avoids having to guess from ambiguous folder depths.

Folder names are matched to Kitsu's names case-insensitively, since
pipeline folder conventions (lowercase) commonly differ from how they're
capitalized in Kitsu ("seq010" vs "SEQ010", "characters" vs "Characters").

When a shot-context scene's folders don't resolve to a Kitsu shot (e.g.
a TV-show project with no episode folder on disk), ShotNotFound is raised
so the panel can offer a manual episode/sequence/shot picker (see
ShotTree). detect_entity and resolve_task are detect_task's two halves,
so a hand-picked shot or task type can replace either one;
detect_with_overrides combines them with the file's remembered manual
picks.

Nothing in this module touches `bpy` -- it only talks to the Kitsu session
it's handed, so it's safe to call from async_utils.run_async's background
thread (and testable outside Blender).
"""

import os

_SHOT_ANCHORS = {"shots", "shot", "sequences", "sequence", "episodes", "episode"}
_ASSET_ANCHORS = {"assets", "asset"}


class TaskDetectionError(Exception):
    pass


class TaskTypeMismatch(TaskDetectionError):
    """Raised when the asset/shot itself resolved fine but its task-type
    folder name didn't match any task type in this project (or that
    entity has no Task of the matched type yet) — callers can offer a
    manual task-type picker instead of just reporting failure.

    `breadcrumb` is the resolved entity's names, top-down, NOT including
    the (unmatched) task type — e.g. ["characters", "Hero"] or
    ["seq010", "sh0100"] or ["ep01", "seq010", "sh0100"]. `context` is
    "asset" or "shot", same as detect_task's own return value, so callers
    can still populate it once a task type is manually picked.
    """

    def __init__(self, entity, breadcrumb, task_type_name, context):
        super().__init__(
            f"Could not match task type '{task_type_name}' for "
            f"{' / '.join(breadcrumb)!r} against this project's task types."
        )
        self.entity = entity
        self.breadcrumb = breadcrumb
        self.task_type_name = task_type_name
        self.context = context


class ShotNotFound(TaskDetectionError):
    """Raised when a scene is in shot context but its folders don't map
    onto a Kitsu episode/sequence/shot (e.g. a TV-show project whose
    files live under `shots/<Seq>/<Shot>/...` with no episode folder) —
    callers can offer a manual shot picker instead of just reporting
    failure.

    `reason` is the original lookup failure, for display. `task_type_name`
    is the folder directly above the file, so once a shot is picked the
    task type can still be resolved from it (see resolve_task).
    """

    def __init__(self, reason, task_type_name):
        super().__init__(reason)
        self.reason = reason
        self.task_type_name = task_type_name


def _folders_above(scene_path, count):
    """Return the `count` folder names directly above the file, ordered
    top-down (outermost first). Raises if any level is missing."""
    if not scene_path:
        raise TaskDetectionError(
            "This scene has not been saved yet, so its folder can't be inspected."
        )

    names = []
    current = scene_path
    for _ in range(count):
        current = os.path.dirname(current)
        names.append(os.path.basename(current))

    names.reverse()
    if not all(names):
        return None
    return names


def _path_is_shot_context(scene_path):
    """True if a shot/sequence anchor folder sits nearer the file than any
    asset anchor; False for an asset anchor or no anchor at all."""
    parts = os.path.normpath(scene_path).replace("\\", "/").split("/")
    # Scan from the file upward (deepest anchor wins) so a studio root that
    # happens to contain both words doesn't override the real context.
    for part in reversed(parts):
        folded = part.strip().casefold()
        if folded in _SHOT_ANCHORS:
            return True
        if folded in _ASSET_ANCHORS:
            return False
    return False


def _find_by_name(items, name):
    target = name.strip().casefold()
    for item in items:
        if (item.get("name") or "").strip().casefold() == target:
            return item
    return None


def task_type_folder_name(scene_path):
    """The folder directly above the file -- by convention, the task type."""
    return os.path.basename(os.path.dirname(scene_path)) or None


def is_shot_context(scene_path):
    return bool(scene_path) and _path_is_shot_context(scene_path)


def _detect_asset(session, project, scene_path):
    names = _folders_above(scene_path, 3)
    if names is None:
        raise TaskDetectionError(
            "Expected an '<AssetType>/<Asset>/<TaskType>/file' folder "
            f"structure above {scene_path!r}, but couldn't find all "
            "three folder levels."
        )
    asset_type_name, asset_name, task_type_name = names

    asset_type = _find_by_name(
        session.all_asset_types_for_project(project), asset_type_name
    )
    if asset_type is None:
        raise TaskDetectionError(
            f"No asset type named '{asset_type_name}' found in this project."
        )

    asset = session.get_asset_by_name(project, asset_name, asset_type=asset_type)
    if asset is None:
        raise TaskDetectionError(
            f"No asset named '{asset_name}' found under asset type "
            f"'{asset_type_name}' in this project."
        )

    return asset, [asset_type_name, asset_name], task_type_name, "asset"


def _has_episodes(project):
    return (project.get("production_type") or "").strip().casefold() == "tvshow"


def _detect_shot(session, project, scene_path):
    episodic = _has_episodes(project)
    # The task-type folder is always the one directly above the file, so
    # it's still known even when the levels above it don't resolve.
    task_type_name = task_type_folder_name(scene_path)

    if episodic:
        names = _folders_above(scene_path, 4)
        if names is None:
            raise ShotNotFound(
                "This is a TV-show project, so an "
                "'<Episode>/<Sequence>/<Shot>/<TaskType>/file' folder "
                f"structure was expected above {scene_path!r}, but not all "
                "four folder levels were found.",
                task_type_name,
            )
        episode_name, sequence_name, shot_name, task_type_name = names
    else:
        names = _folders_above(scene_path, 3)
        if names is None:
            raise ShotNotFound(
                "Expected a '<Sequence>/<Shot>/<TaskType>/file' folder "
                f"structure above {scene_path!r}, but couldn't find all "
                "three folder levels.",
                task_type_name,
            )
        episode_name = None
        sequence_name, shot_name, task_type_name = names

    episode = None
    if episodic:
        episode = session.get_episode_by_name(project, episode_name)
        if episode is None:
            raise ShotNotFound(
                f"No episode named '{episode_name}' found in this project.",
                task_type_name,
            )

    sequence = session.get_sequence_by_name(project, sequence_name, episode=episode)
    if sequence is None:
        where = f"episode '{episode_name}'" if episodic else "this project"
        raise ShotNotFound(
            f"No sequence named '{sequence_name}' found in {where}.",
            task_type_name,
        )

    shot = session.get_shot_by_name(sequence, shot_name)
    if shot is None:
        raise ShotNotFound(
            f"No shot named '{shot_name}' found under sequence "
            f"'{sequence_name}'.",
            task_type_name,
        )

    breadcrumb = [sequence_name, shot_name]
    if episodic:
        breadcrumb.insert(0, episode_name)

    return shot, breadcrumb, task_type_name, "shot"


def detect_entity(session, project, scene_path):
    """First half of detect_task: resolve only the asset/shot.

    Returns (entity, breadcrumb, task_type_name, context) -- breadcrumb
    being the entity's names only, and task_type_name the (not yet
    matched) task-type folder name. Raises ShotNotFound for a shot-context
    scene whose folders don't resolve, TaskDetectionError otherwise.
    """
    if not scene_path:
        raise TaskDetectionError(
            "This scene has not been saved yet, so its folder can't be inspected."
        )

    if _path_is_shot_context(scene_path):
        return _detect_shot(session, project, scene_path)
    return _detect_asset(session, project, scene_path)


def resolve_task(session, project, entity, breadcrumb, task_type_name, context):
    """Second half of detect_task: find `entity`'s task matching the
    task-type folder name. Raises TaskTypeMismatch when there's no
    matching task type, or the entity has no task of that type."""
    if not task_type_name:
        raise TaskTypeMismatch(entity, breadcrumb, "", context)

    task_type = _find_by_name(
        session.all_task_types_for_project(project), task_type_name
    )
    if task_type is None:
        raise TaskTypeMismatch(entity, breadcrumb, task_type_name, context)

    task = session.get_task_by_entity(entity, task_type)
    if task is None:
        raise TaskTypeMismatch(entity, breadcrumb, task_type_name, context)

    return task, breadcrumb + [task_type_name], context


def resolve_task_by_type_id(session, project, entity, breadcrumb, context, task_type_id):
    """Like resolve_task, but for a task type picked by hand (by id).
    Returns None instead of raising when the type no longer exists or
    the entity has no task of that type, so callers can fall back to
    normal folder matching."""
    task_type = next(
        (
            t
            for t in session.all_task_types_for_project(project)
            if t.get("id") == task_type_id
        ),
        None,
    )
    if task_type is None:
        return None
    task = session.get_task_by_entity(entity, task_type)
    if task is None:
        return None
    return task, breadcrumb + [task_type.get("name", "?")], context


class ShotTree:
    """The project's episodes, sequences and shots, for the manual
    episode -> sequence -> shot picker. Built from three bulk requests
    (see load) rather than per-shot lookups; shots link to their
    sequence and sequences to their episode via `parent_id`."""

    def __init__(self, episodes, sequences, shots):
        def by_name(items):
            return sorted(items, key=lambda i: (i.get("name") or "").casefold())

        self._episodes = by_name(episodes)
        self._sequences = by_name(sequences)
        self._shots = by_name(shots)
        self._episodes_by_id = {e["id"]: e for e in self._episodes}
        self._sequences_by_id = {s["id"]: s for s in self._sequences}
        self._shots_by_id = {s["id"]: s for s in self._shots}

    @classmethod
    def load(cls, session, project):
        return cls(
            session.all_episodes_for_project(project) or [],
            session.all_sequences_for_project(project) or [],
            session.all_shots_for_project(project) or [],
        )

    @property
    def episodic(self):
        return bool(self._episodes)

    def episodes(self):
        return list(self._episodes)

    def sequences(self, episode_id=None):
        """Sequences of `episode_id`, or all of them for a project
        without episodes."""
        if not self.episodic:
            return list(self._sequences)
        return [s for s in self._sequences if s.get("parent_id") == episode_id]

    def shots(self, sequence_id):
        return [s for s in self._shots if s.get("parent_id") == sequence_id]

    def find_shot(self, shot_id):
        return self._shots_by_id.get(shot_id)

    def parents(self, shot):
        """(episode, sequence) dicts of `shot`; either may be None."""
        sequence = self._sequences_by_id.get(shot.get("parent_id"))
        episode = None
        if sequence is not None:
            episode = self._episodes_by_id.get(sequence.get("parent_id"))
        return episode, sequence

    def breadcrumb(self, shot):
        episode, sequence = self.parents(shot)
        names = [shot.get("name", "?")]
        if sequence is not None:
            names.insert(0, sequence.get("name", "?"))
        if episode is not None:
            names.insert(0, episode.get("name", "?"))
        return names


def detect_task(session, project, scene_path):
    """Resolve the Kitsu task matching the scene's folder structure.

    Handles both asset and shot contexts (see the module docstring for the
    expected folder layouts).

    Returns:
        tuple: (task, breadcrumb, context) where breadcrumb is a list of
        the resolved entity names, top-down, for display (e.g.
        ["characters", "Hero", "modeling"] or
        ["ep01", "seq010", "sh0100", "animation"]), and context is
        "asset" or "shot".
    """
    entity, breadcrumb, task_type_name, context = detect_entity(
        session, project, scene_path
    )
    return resolve_task(session, project, entity, breadcrumb, task_type_name, context)


def detect_with_overrides(session, project, scene_path, shot_id, task_type_id):
    """Folder detection plus the scene's remembered manual picks, which
    win over the folders. Runs on async_utils.run_async's background
    thread: network and plain Python only, never `bpy` -- the caller reads
    the remembered ids (blender_scene.load_shot_override /
    load_task_type_override) on the main thread beforehand.

    Returns a dict -- `entity`, `breadcrumb`, `context` and
    `task_type_name` (the folder name) describe what the task picker
    would apply to; `result` is detect_task's (task, breadcrumb, context)
    or None with `error` saying why; `manual` is True when a remembered
    pick was used; `shot_tree` is the loaded ShotTree, if one was needed.
    Raises TaskDetectionError for an asset that can't be found.
    """
    outcome = {
        "entity": None,
        "breadcrumb": None,
        "context": None,
        "task_type_name": task_type_folder_name(scene_path) if scene_path else None,
        "shot_tree": None,
        "result": None,
        "error": None,
        "manual": False,
    }
    try:
        entity, breadcrumb, task_type_name, context = detect_entity(
            session, project, scene_path
        )
        outcome.update(
            entity=entity, breadcrumb=breadcrumb, task_type_name=task_type_name, context=context
        )
    except ShotNotFound as exc:
        outcome.update(context="shot", error=exc.reason)

    if outcome["context"] == "shot" and (shot_id or outcome["entity"] is None):
        tree = outcome["shot_tree"] = ShotTree.load(session, project)
        shot = tree.find_shot(shot_id) if shot_id else None
        if shot is not None:
            outcome.update(entity=shot, breadcrumb=tree.breadcrumb(shot), manual=True, error=None)

    if outcome["entity"] is None:
        return outcome

    if task_type_id:
        result = resolve_task_by_type_id(
            session, project, outcome["entity"], outcome["breadcrumb"],
            outcome["context"], task_type_id,
        )
        if result is not None:
            outcome.update(result=result, manual=True)
            return outcome

    try:
        outcome["result"] = resolve_task(
            session, project, outcome["entity"], outcome["breadcrumb"],
            outcome["task_type_name"], outcome["context"],
        )
    except TaskTypeMismatch:
        outcome["error"] = (
            " / ".join(outcome["breadcrumb"])
            + f" — folder task type '{outcome['task_type_name']}' has no matching task in Kitsu"
        )
    return outcome

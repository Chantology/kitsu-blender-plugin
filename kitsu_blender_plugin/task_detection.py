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
project the extra `<Episode>` level is expected and for a movie/short it is
not. This avoids having to guess from ambiguous folder depths.

Folder names are matched to Kitsu's names case-insensitively, since
pipeline folder conventions (lowercase) commonly differ from how they're
capitalized in Kitsu ("seq010" vs "SEQ010", "characters" vs "Characters").
"""

import os

_SHOT_ANCHORS = {"shots", "shot", "sequences", "sequence", "episodes", "episode"}
_ASSET_ANCHORS = {"assets", "asset"}


class TaskDetectionError(Exception):
    pass


class TaskTypeMismatch(TaskDetectionError):
    """Raised when the asset/shot resolved fine but its task-type folder
    name didn't match any task type in Kitsu (or that entity has no Task
    of the matched type yet) — callers can offer a manual task-type
    picker instead of just reporting failure."""

    def __init__(self, entity, breadcrumb, task_type_name, context):
        super().__init__(
            f"Could not match task type {task_type_name!r} for "
            f"{' / '.join(breadcrumb)!r} against this project's task types."
        )
        self.entity = entity
        self.breadcrumb = breadcrumb  # entity breadcrumb only -- task type name not included
        self.task_type_name = task_type_name
        self.context = context  # "asset" | "shot"


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


def _resolve_task_type(session, project, task_type_name):
    task_type = _find_by_name(
        session.all_task_types_for_project(project), task_type_name
    )
    if task_type is None:
        raise TaskDetectionError(
            f"No task type named '{task_type_name}' found in this project."
        )
    return task_type


def _detect_asset_task(session, project, scene_path):
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

    entity_breadcrumb = [asset_type_name, asset_name]

    try:
        task_type = _resolve_task_type(session, project, task_type_name)
    except TaskDetectionError:
        raise TaskTypeMismatch(asset, entity_breadcrumb, task_type_name, "asset")

    task = session.get_task_by_entity(asset, task_type)
    if task is None:
        raise TaskTypeMismatch(asset, entity_breadcrumb, task_type_name, "asset")

    return task, entity_breadcrumb + [task_type_name], "asset"


def _has_episodes(project):
    return (project.get("production_type") or "").strip().casefold() == "tvshow"


def _detect_shot_task(session, project, scene_path):
    episodic = _has_episodes(project)

    if episodic:
        names = _folders_above(scene_path, 4)
        if names is None:
            raise TaskDetectionError(
                "This is a TV-show project, so an "
                "'<Episode>/<Sequence>/<Shot>/<TaskType>/file' folder "
                f"structure was expected above {scene_path!r}, but not all "
                "four folder levels were found."
            )
        episode_name, sequence_name, shot_name, task_type_name = names
    else:
        names = _folders_above(scene_path, 3)
        if names is None:
            raise TaskDetectionError(
                "Expected a '<Sequence>/<Shot>/<TaskType>/file' folder "
                f"structure above {scene_path!r}, but couldn't find all "
                "three folder levels."
            )
        episode_name = None
        sequence_name, shot_name, task_type_name = names

    episode = None
    if episodic:
        episode = session.get_episode_by_name(project, episode_name)
        if episode is None:
            raise TaskDetectionError(
                f"No episode named '{episode_name}' found in this project."
            )

    sequence = session.get_sequence_by_name(project, sequence_name, episode=episode)
    if sequence is None:
        where = f"episode '{episode_name}'" if episodic else "this project"
        raise TaskDetectionError(
            f"No sequence named '{sequence_name}' found in {where}."
        )

    shot = session.get_shot_by_name(sequence, shot_name)
    if shot is None:
        raise TaskDetectionError(
            f"No shot named '{shot_name}' found under sequence "
            f"'{sequence_name}'."
        )

    entity_breadcrumb = [sequence_name, shot_name]
    if episodic:
        entity_breadcrumb.insert(0, episode_name)

    try:
        task_type = _resolve_task_type(session, project, task_type_name)
    except TaskDetectionError:
        raise TaskTypeMismatch(shot, entity_breadcrumb, task_type_name, "shot")

    task = session.get_task_by_entity(shot, task_type)
    if task is None:
        raise TaskTypeMismatch(shot, entity_breadcrumb, task_type_name, "shot")

    return task, entity_breadcrumb + [task_type_name], "shot"


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
    if not scene_path:
        raise TaskDetectionError(
            "This scene has not been saved yet, so its folder can't be inspected."
        )

    if _path_is_shot_context(scene_path):
        return _detect_shot_task(session, project, scene_path)
    return _detect_asset_task(session, project, scene_path)

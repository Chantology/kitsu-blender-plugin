from __future__ import annotations

from . import user as gazu_user
from . import project as gazu_project
from . import asset as gazu_asset
from . import task as gazu_task
from . import shot as gazu_shot
from . import scene as gazu_scene
from . import client as raw
from .client import KitsuClient

default = raw.default_client


def all_open_projects(
    user_context: bool = False, client: KitsuClient = default
) -> list[dict]:
    """
    Return open projects (only the current user's when user_context is True).
    """
    if user_context:
        return gazu_user.all_open_projects(client=client)
    else:
        return gazu_project.all_open_projects(client=client)


def all_assets_for_project(
    project: str | dict,
    client: KitsuClient = default,
) -> list[dict]:
    """
    Return the project's assets.
    """
    return gazu_asset.all_assets_for_project(project, client=client)


def all_asset_types_for_project(
    project: str | dict,
    user_context: bool = False,
    client: KitsuClient = default,
) -> list[dict]:
    """
    Return the project's asset types (only the current user's when
    user_context is True).
    """
    if user_context:
        return gazu_user.all_asset_types_for_project(project, client=client)
    else:
        return gazu_asset.all_asset_types_for_project(project, client=client)


def all_assets_for_asset_type_and_project(
    project: str | dict,
    asset_type: str | dict,
    user_context: bool = False,
    client: KitsuClient = default,
) -> list[dict]:
    """
    Return the assets for given project and asset type (only the current
    user's when user_context is True).
    """
    if user_context:
        return gazu_user.all_assets_for_asset_type_and_project(
            project, asset_type, client=client
        )
    else:
        return gazu_asset.all_assets_for_project_and_type(
            project, asset_type, client=client
        )


def all_task_types_for_asset(
    asset: str | dict,
    user_context: bool = False,
    client: KitsuClient = default,
) -> list[dict]:
    """
    Return the task types for given asset (only the current user's when
    user_context is True).
    """
    if user_context:
        return gazu_user.all_task_types_for_asset(asset, client=client)
    else:
        return gazu_task.all_task_types_for_asset(asset, client=client)


def all_task_types_for_shot(
    shot: str | dict,
    user_context: bool = False,
    client: KitsuClient = default,
) -> list[dict]:
    """
    Return the task types for given shot (only the current user's when
    user_context is True).
    """
    if user_context:
        return gazu_user.all_task_types_for_shot(shot, client=client)
    else:
        return gazu_task.all_task_types_for_shot(shot, client=client)


def all_task_types_for_scene(
    scene: str | dict,
    user_context: bool = False,
    client: KitsuClient = default,
) -> list[dict]:
    """
    Return the task types for given scene (only the current user's when
    user_context is True).
    """
    if user_context:
        return gazu_user.all_task_types_for_scene(scene, client=client)
    else:
        return gazu_task.all_task_types_for_scene(scene, client=client)


def all_task_types_for_sequence(
    sequence: str | dict,
    user_context: bool = False,
    client: KitsuClient = default,
) -> list[dict]:
    """
    Return the task types for given sequence (only the current user's when
    user_context is True).
    """
    if user_context:
        return gazu_user.all_task_types_for_sequence(sequence, client=client)
    else:
        return gazu_task.all_task_types_for_sequence(sequence, client=client)


def all_sequences_for_project(
    project: str | dict,
    user_context: bool = False,
    client: KitsuClient = default,
) -> list[dict]:
    """
    Return the project's sequences (only the current user's when user_context
    is True).
    """
    if user_context:
        return gazu_user.all_sequences_for_project(project, client=client)
    else:
        return gazu_shot.all_sequences_for_project(project, client=client)


def all_scenes_for_project(
    project: str | dict,
    client: KitsuClient = default,
) -> list[dict]:
    """
    Return the project's scenes.
    """
    return gazu_scene.all_scenes(project, client=client)


def all_shots_for_sequence(
    sequence: str | dict,
    user_context: bool = False,
    client: KitsuClient = default,
) -> list[dict]:
    """
    Return the sequence's shots (only the current user's when user_context is
    True).
    """
    if user_context:
        return gazu_user.all_shots_for_sequence(sequence, client=client)
    else:
        return gazu_shot.all_shots_for_sequence(sequence, client=client)


def all_scenes_for_sequence(
    sequence: str | dict,
    user_context: bool = False,
    client: KitsuClient = default,
) -> list[dict]:
    """
    Return the sequence's scenes (only the current user's when user_context is
    True).
    """
    if user_context:
        return gazu_user.all_scenes_for_sequence(sequence, client=client)
    else:
        return gazu_scene.all_scenes_for_sequence(sequence, client=client)


def all_sequences_for_episode(
    episode: str | dict,
    client: KitsuClient = default,
) -> list[dict]:
    """
    Return the episode's sequences.
    """
    return gazu_shot.all_sequences_for_episode(episode, client=client)


def all_episodes_for_project(
    project: str | dict,
    user_context: bool = False,
    client: KitsuClient = default,
) -> list[dict]:
    """
    Return the project's episodes (only the current user's when user_context
    is True).
    """
    if user_context:
        return gazu_user.all_episodes_for_project(project, client=client)
    else:
        return gazu_shot.all_episodes_for_project(project, client=client)

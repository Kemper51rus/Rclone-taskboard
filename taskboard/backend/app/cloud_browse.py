from __future__ import annotations

import json
import subprocess
from typing import Any

from .domain import CloudSettings


def _normalize_cloud_browse_path(path: str | None) -> str:
    if path is None:
        return ""
    return "/".join(segment for segment in str(path).strip().strip("/").split("/") if segment and segment != ".")


def _join_cloud_path(root_path: str | None, path: str | None) -> str:
    root = _normalize_cloud_browse_path(root_path)
    subpath = _normalize_cloud_browse_path(path)
    return "/".join(segment for segment in [root, subpath] if segment)


def _cloud_browse_remote_path(cloud: CloudSettings, path: str | None) -> str:
    remote_name = (cloud.remote_name or "").strip()
    if not remote_name:
        raise ValueError("cloud has no rclone remote name")
    joined_path = _join_cloud_path(cloud.root_path, path)
    return f"{remote_name}:/{joined_path}" if joined_path else f"{remote_name}:"


def _parent_cloud_path(path: str) -> str | None:
    normalized = _normalize_cloud_browse_path(path)
    if not normalized:
        return None
    parent = "/".join(normalized.split("/")[:-1])
    return parent


def _browse_cloud_directories(cloud: CloudSettings, path: str | None = None) -> dict[str, Any]:
    normalized_path = _normalize_cloud_browse_path(path)
    remote_path = _cloud_browse_remote_path(cloud, normalized_path)
    completed = subprocess.run(
        ["rclone", "lsjson", "--dirs-only", remote_path],
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or f"rclone lsjson failed with code {completed.returncode}")
    try:
        raw_items = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("rclone lsjson returned invalid JSON") from exc
    if not isinstance(raw_items, list):
        raise RuntimeError("rclone lsjson returned unexpected payload")

    directories: list[dict[str, str]] = []
    for item in raw_items:
        if not isinstance(item, dict) or item.get("IsDir") is not True:
            continue
        name = str(item.get("Name") or "").strip().strip("/")
        if not name:
            item_path = _normalize_cloud_browse_path(item.get("Path"))
            name = item_path.rsplit("/", 1)[-1] if item_path else ""
        if not name:
            continue
        child_path = "/".join(segment for segment in [normalized_path, name] if segment)
        directories.append(
            {
                "name": name,
                "path": child_path,
                "remote_path": _cloud_browse_remote_path(cloud, child_path),
            }
        )

    directories.sort(key=lambda item: item["name"].lower())
    return {
        "cloud_key": cloud.key,
        "path": normalized_path,
        "parent": _parent_cloud_path(normalized_path),
        "remote_path": remote_path,
        "directories": directories,
    }

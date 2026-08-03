"""Release checks and distribution metadata for VideoCrush."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from videocrush_core import VERSION, VideoCrushError


DEFAULT_REPOSITORY = "SysAdminDoc/VideoCrush"


@dataclass(frozen=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    update_available: bool
    release_url: str = ""
    release_name: str = ""


def _version_tuple(value: str) -> Tuple[int, ...]:
    parts = re.findall(r"\d+", str(value).lstrip("vV"))
    if not parts:
        raise VideoCrushError(f"Invalid release version: {value}")
    return tuple(int(part) for part in parts)


def check_for_update(
    repository: str = DEFAULT_REPOSITORY,
    current_version: str = VERSION,
    timeout: float = 5.0,
    opener: Optional[Callable] = None,
) -> UpdateInfo:
    """Check the latest public GitHub release without downloading or installing it."""

    url = f"https://api.github.com/repos/{repository}/releases/latest"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "VideoCrush"})
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, TypeError, urllib.error.URLError) as exc:
        raise VideoCrushError(f"Could not check releases: {exc}") from exc
    latest = str(payload.get("tag_name", "")).lstrip("vV")
    if not latest:
        raise VideoCrushError("The release service returned no latest version.")
    return UpdateInfo(
        current_version=current_version,
        latest_version=latest,
        update_available=_version_tuple(latest) > _version_tuple(current_version),
        release_url=str(payload.get("html_url", "")),
        release_name=str(payload.get("name", "")),
    )

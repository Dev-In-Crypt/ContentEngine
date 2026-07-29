"""Where the media library's files live: uploads/media/<user_id>/<asset_id>.<ext>.

Mirrors logo_store and music_store — keyed by the tenant, path always built
server-side, every read re-checks containment so a client never names a file on
our disk. Two things differ from staging, and both follow from what a media
asset is:

**The id comes in, it is not minted here.** A `MediaAsset` row is the identity;
the file is a payload that arrives later. A generated video's row exists from
the moment it is requested and its bytes land minutes afterwards, once the
provider finishes and the poller downloads them. So `path_for` on an asset with
no file yet answers "absent" rather than raising — "still rendering" and "you
asked for something malformed" are different answers, and only the second is a
refusal.

**The sweep is orphan-based, not age-based.** These files are permanent until
their row goes away; a month-old asset the user still owns must survive. It
also refuses to touch anything that doesn't carry an asset id, because deleting
what it doesn't recognise is how a sweep turns into data loss.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Optional

#: backend/services/media_store.py -> backend/uploads/media
MEDIA_ROOT = Path(__file__).resolve().parent.parent / "uploads" / "media"

#: Content type → stored extension. Also the allow-list.
EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "video/mp4": "mp4",
}

#: A MediaAsset id is str(uuid4) — dashed, unlike staging's 32-char hex.
_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

#: User.id is a uuid too, but the tests use readable stand-ins ("user-1"), so
#: this allows any single path segment while refusing separators and dot-dot.
_USER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class MediaError(Exception):
    """Unsupported type, a malformed id, or a path that escapes the folder."""


def user_dir(user_id: str, root: Optional[Path] = None) -> Path:
    """One folder per tenant, so one user's id can never name another's file.

    The user id is checked, not just the asset id: it is the *first* segment of
    every path this module builds, so a `user_id` of "../.." walks out of the
    media root before an asset id is even considered. It always arrives from an
    authenticated session today — but a store is a library function and must
    not depend on who happens to call it.
    """
    base = (root or MEDIA_ROOT).resolve()
    if not _USER_RE.match(str(user_id or "")):
        raise MediaError("Invalid user id")
    directory = (base / str(user_id)).resolve()
    if not directory.is_relative_to(base):
        raise MediaError("Invalid user id")
    return directory


def _checked_dir(user_id: str, asset_id: str, root: Optional[Path]) -> Path:
    """Resolve the tenant's folder after refusing a malformed asset id.

    The id shape is checked before the filesystem is touched, so "../.." never
    reaches it. The per-candidate containment check below is a second lock that
    is unreachable while this regex holds — kept deliberately, so that loosening
    the regex later cannot silently open a traversal.
    """
    if not _ID_RE.match(asset_id or ""):
        raise MediaError("Unknown asset id")
    return user_dir(user_id, root)


def _candidates(directory: Path, asset_id: str) -> Iterable[Path]:
    for ext in dict.fromkeys(EXTENSIONS.values()):
        candidate = (directory / f"{asset_id}.{ext}").resolve()
        if not candidate.is_relative_to(directory):
            raise MediaError("Unknown asset id")
        yield candidate


def _prepare_target(user_id: str, asset_id: str, content_type: str,
                    root: Optional[Path]) -> Path:
    """Resolve the path an asset's file belongs at, clearing whatever was there.

    Shared by save() and adopt_file() so the two ways bytes can arrive — held
    in memory, or already streamed to a temp file on disk — end up written by
    exactly one path-construction and one write-clears-old-extension rule.
    """
    ext = EXTENSIONS.get(content_type)
    if ext is None:
        raise MediaError(f"Unsupported content type {content_type!r}")
    directory = _checked_dir(user_id, asset_id, root)
    directory.mkdir(parents=True, exist_ok=True)
    # An edited clip re-saved as mp4 must not sit next to its own jpg.
    delete(user_id, asset_id, root)
    target = (directory / f"{asset_id}.{ext}").resolve()
    if not target.is_relative_to(directory):
        raise MediaError("Unknown asset id")
    return target


def save(user_id: str, asset_id: str, data: bytes, content_type: str,
         root: Optional[Path] = None) -> Path:
    """Store the bytes for an existing asset row; return the path written."""
    target = _prepare_target(user_id, asset_id, content_type, root)
    target.write_bytes(data)
    return target


def adopt_file(user_id: str, asset_id: str, tmp_path: Path, content_type: str,
              root: Optional[Path] = None) -> Path:
    """Move an already-written file into the store; return the path written.

    For an upload streamed to a temp file in chunks rather than read whole into
    memory — a video-sized file is large enough that `save()`'s in-memory bytes
    would be a real cost, and there is no reason to read it a second time just
    to write it once.
    """
    target = _prepare_target(user_id, asset_id, content_type, root)
    os.replace(tmp_path, target)
    return target


def path_for(user_id: str, asset_id: str,
             root: Optional[Path] = None) -> Optional[Path]:
    """The asset's file, or None when it has not been written yet."""
    directory = _checked_dir(user_id, asset_id, root)
    for candidate in _candidates(directory, asset_id):
        if candidate.exists():
            return candidate
    return None


def delete(user_id: str, asset_id: str, root: Optional[Path] = None) -> None:
    """Remove the asset's file if present. No error if there is none."""
    directory = _checked_dir(user_id, asset_id, root)
    for candidate in _candidates(directory, asset_id):
        candidate.unlink(missing_ok=True)


def sweep(live_ids: set[str], root: Optional[Path] = None) -> dict:
    """Delete files whose asset row is gone. Returns {files, bytes}.

    `live_ids` is every asset id still in the database. Anything on disk that
    carries an asset-id filename and is not in that set has lost its row and is
    unreachable; anything that doesn't look like an asset id is left alone.
    """
    directory = root or MEDIA_ROOT
    if not directory.exists():
        return {"files": 0, "bytes": 0}
    files = freed = 0
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        if not _ID_RE.match(path.stem):
            continue
        if path.stem in live_ids:
            continue
        freed += path.stat().st_size
        path.unlink(missing_ok=True)
        files += 1
    return {"files": files, "bytes": freed}

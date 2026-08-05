"""The tenant's brand logo — one persistent file per user, drawn on every slide.

Unlike staging (a scratch area swept after a day), a logo lives until the tenant
replaces or removes it. It is keyed by user id, the path is always built from that
id server-side, and every read re-checks the resolved path is inside the logo
folder — a client never names a file on our disk.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

#: backend/services/logo_store.py -> backend/uploads/logos
LOGO_ROOT = Path(__file__).resolve().parent.parent / "uploads" / "logos"

#: Content type → stored extension. Also the allow-list.
EXTENSIONS = {"image/png": "png", "image/webp": "webp", "image/jpeg": "jpg"}


class LogoError(Exception):
    """Unsupported type, or a path that escapes the logo folder."""


def _root(root: Optional[Path] = None) -> Path:
    return (root or LOGO_ROOT).resolve()


def save(user_id: str, data: bytes, content_type: str,
         root: Optional[Path] = None) -> Path:
    """Store the tenant's logo, replacing any previous one, and return its path."""
    ext = EXTENSIONS.get(content_type)
    if ext is None:
        raise LogoError(f"Unsupported content type {content_type!r}")
    directory = _root(root)
    directory.mkdir(parents=True, exist_ok=True)
    # Drop any prior logo first, so a new .png doesn't sit next to an old .jpg and
    # leave path_for() picking the stale one.
    delete(user_id, root)
    target = directory / f"{user_id}.{ext}"
    target.write_bytes(data)
    return target


def path_for(user_id: str, root: Optional[Path] = None) -> Optional[Path]:
    """The tenant's logo file, or None. Refuses anything outside the logo folder."""
    directory = _root(root)
    for ext in EXTENSIONS.values():
        candidate = (directory / f"{user_id}.{ext}").resolve()
        if not candidate.is_relative_to(directory):
            raise LogoError("Invalid logo path")
        if candidate.exists():
            return candidate
    return None


def resolve(stored_path: Optional[str], root: Optional[Path] = None) -> Optional[Path]:
    """A logo_path out of the database as a real file, or None.

    Every value we store here came from save(), so it is contained by
    construction — but it is still a database string reaching the filesystem,
    and the containment check costs nothing. Same guard as path_for's, applied
    to a full path rather than a key.
    """
    if not stored_path:
        return None
    directory = _root(root)
    candidate = Path(stored_path).resolve()
    if not candidate.is_relative_to(directory):
        raise LogoError("Invalid logo path")
    return candidate if candidate.exists() else None


def delete(user_id: str, root: Optional[Path] = None) -> None:
    """Remove the tenant's logo if present. No error if there is none."""
    existing = path_for(user_id, root)
    if existing:
        existing.unlink(missing_ok=True)

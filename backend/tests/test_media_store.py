"""The media library's files on disk: one tenant can never read another's.

Same containment discipline as staging, with two differences that matter. Ids
here are minted by the database row, not by the store — a video asset exists
from the moment it is requested and its file arrives minutes later, so the id
has to come in rather than out. And the sweep is orphan-based, not age-based:
these files are permanent until their row goes away.
"""
import pytest

from services import media_store
from services.media_store import MediaError

ASSET = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
OTHER = "9c858901-8a57-4791-81fe-4c455b099bc9"


def test_save_returns_a_path_that_reads_back(tmp_path):
    path = media_store.save("user-1", ASSET, b"jpeg-bytes", "image/jpeg", root=tmp_path)
    assert path.read_bytes() == b"jpeg-bytes"
    assert media_store.path_for("user-1", ASSET, root=tmp_path) == path


def test_the_file_is_named_by_the_asset_id_inside_the_users_folder(tmp_path):
    media_store.save("user-1", ASSET, b"x", "image/png", root=tmp_path)
    assert (tmp_path / "user-1" / f"{ASSET}.png").exists()


def test_video_is_a_first_class_kind(tmp_path):
    """The whole point of the library — a generated clip has to live here too."""
    media_store.save("user-1", ASSET, b"mp4-bytes", "video/mp4", root=tmp_path)
    assert (tmp_path / "user-1" / f"{ASSET}.mp4").exists()


def test_another_users_id_does_not_resolve(tmp_path):
    """The isolation story: same id, different tenant, no read."""
    media_store.save("user-1", ASSET, b"private", "image/jpeg", root=tmp_path)
    assert media_store.path_for("user-2", ASSET, root=tmp_path) is None


def test_an_asset_with_no_file_yet_is_absent_not_an_error(tmp_path):
    """A video row exists while the provider is still rendering. "Not here yet"
    and "you asked for something malformed" are different answers, and only the
    second one is a refusal."""
    assert media_store.path_for("user-1", ASSET, root=tmp_path) is None


@pytest.mark.parametrize("bad", [
    "../../../etc/passwd",
    "..\\..\\windows\\win.ini",
    "abc",
    "",
    "3f2504e04f8911d39a0c0305e82c3301",     # staging's shape — not ours
    "gggggggg-4f89-11d3-9a0c-0305e82c3301",  # right shape, not hex
])
def test_malformed_ids_are_refused(tmp_path, bad):
    media_store.save("user-1", ASSET, b"x", "image/jpeg", root=tmp_path)
    with pytest.raises(MediaError):
        media_store.path_for("user-1", bad, root=tmp_path)


@pytest.mark.parametrize("bad_user", ["../..", "..", "a/../..", ""])
def test_a_user_id_that_escapes_the_media_root_is_refused(tmp_path, bad_user):
    """The asset id is not the only thing interpolated into the path — the user
    id is the first segment. It always arrives from an authenticated session
    today, but a store is a library function and must not assume its caller."""
    with pytest.raises(MediaError):
        media_store.path_for(bad_user, ASSET, root=tmp_path)
    with pytest.raises(MediaError):
        media_store.save(bad_user, ASSET, b"x", "image/jpeg", root=tmp_path)


def test_traversal_to_a_file_that_really_exists_is_refused(tmp_path):
    """The teeth of the check: an id that escapes the user folder and lands on a
    file that IS there must still be refused, not served."""
    (tmp_path / "secret.mp4").write_bytes(b"another tenant's video")
    media_store.save("user-1", ASSET, b"mine", "video/mp4", root=tmp_path)
    with pytest.raises(MediaError):
        media_store.path_for("user-1", "../secret", root=tmp_path)


def test_saving_under_a_malformed_id_is_refused(tmp_path):
    """The guard belongs on the write path too — otherwise a bad id never has to
    survive path_for to do damage."""
    with pytest.raises(MediaError):
        media_store.save("user-1", "../escape", b"x", "image/jpeg", root=tmp_path)


def test_unsupported_content_type_is_refused(tmp_path):
    with pytest.raises(MediaError):
        media_store.save("user-1", ASSET, b"%PDF", "application/pdf", root=tmp_path)


def test_replacing_an_asset_does_not_leave_the_old_extension_behind(tmp_path):
    """An edited clip re-saved as mp4 must not sit next to its own jpg."""
    media_store.save("user-1", ASSET, b"first", "image/jpeg", root=tmp_path)
    media_store.save("user-1", ASSET, b"second", "video/mp4", root=tmp_path)
    assert not (tmp_path / "user-1" / f"{ASSET}.jpg").exists()
    assert media_store.path_for("user-1", ASSET, root=tmp_path).read_bytes() == b"second"


def test_delete_removes_the_file_and_is_quiet_when_there_is_none(tmp_path):
    media_store.save("user-1", ASSET, b"x", "image/jpeg", root=tmp_path)
    media_store.delete("user-1", ASSET, root=tmp_path)
    assert media_store.path_for("user-1", ASSET, root=tmp_path) is None
    media_store.delete("user-1", ASSET, root=tmp_path)   # no error the second time


def test_sweep_removes_files_whose_row_is_gone_and_keeps_the_rest(tmp_path):
    """Orphan-based, not age-based: a month-old asset the user still owns must
    survive, and a minute-old one whose row was deleted must not."""
    media_store.save("user-1", ASSET, b"live", "image/jpeg", root=tmp_path)
    media_store.save("user-1", OTHER, b"orphan", "video/mp4", root=tmp_path)

    result = media_store.sweep({ASSET}, root=tmp_path)

    assert result["files"] == 1
    assert media_store.path_for("user-1", ASSET, root=tmp_path) is not None
    assert media_store.path_for("user-1", OTHER, root=tmp_path) is None


def test_sweep_reports_the_bytes_it_freed(tmp_path):
    media_store.save("user-1", OTHER, b"0123456789", "video/mp4", root=tmp_path)
    assert media_store.sweep(set(), root=tmp_path)["bytes"] == 10


def test_sweep_ignores_files_it_does_not_recognise(tmp_path):
    """A stray file with no asset-id shape is not ours to delete — deleting the
    unrecognised is how a sweep turns into data loss."""
    (tmp_path / "user-1").mkdir()
    (tmp_path / "user-1" / "notes.txt").write_bytes(b"hands off")
    assert media_store.sweep(set(), root=tmp_path)["files"] == 0
    assert (tmp_path / "user-1" / "notes.txt").exists()


def test_sweep_on_a_missing_root_is_a_no_op(tmp_path):
    assert media_store.sweep(set(), root=tmp_path / "nope") == {"files": 0, "bytes": 0}

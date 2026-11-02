"""Shared POSIX credential persistence. All token writers must participate."""
from contextlib import contextmanager
import json
import os
import stat
import tempfile
from pathlib import Path

class CallbackError(Exception):
    """Secret-free persistence failure."""

def file_version(path):
    """Inspect metadata only; never open the existing credential contents."""
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise CallbackError("Credential destination must be a regular file")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_gid)


@contextmanager
def writer_lock(directory, *, wait=False):
    """Cooperative POSIX lock on a stable sibling lock file; never unlink it.

    Only participating writers are excluded. This is not a deployment gate.
    The isolated directory must be private, stable, and on a local filesystem.
    """
    if os.name != "posix":
        raise CallbackError("Isolated writer locking requires POSIX")
    import fcntl

    fd = os.open(Path(directory) / ".google-token.lock",
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise CallbackError("Credential writer busy") from None
        yield
    finally:
        os.close(fd)  # Closing also releases the lock after interruption.


def atomic_save(path, payload, *, expected_version):
    """Isolated cooperative transaction; never truncate or restore over a writer.

    Public callback login remains gated. Nonparticipating external writers
    cannot be excluded by an advisory lock.
    """
    path = Path(path)
    with writer_lock(path.parent):
        _save_locked(path, payload, expected_version=expected_version)


def _save_locked(path, payload, *, expected_version):
    fd, name = tempfile.mkstemp(prefix=".google-oauth-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if file_version(path) != expected_version:
            raise CallbackError("Credential destination changed")
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def credential_transaction(path, operation, *, read_existing=True, backup=False):
    """Hold cooperative exclusion before read, throughout refresh and commit.

    operation receives the current JSON (or None for replacement) and returns
    the complete new JSON. Restoration uses the same protocol with an approved
    backup payload. Refresh entry points use refresh_session to retain the lock
    across credential loading, transport and persistence. Nonparticipants remain outside this protocol.
    """
    path = Path(path)
    with writer_lock(path.parent):
        version = file_version(path)
        current = None
        if read_existing and version is not None:
            current = json.loads(path.read_text(encoding="utf-8"))
        payload = operation(current)
        if backup and version is not None:
            backup_path = path.with_name(path.name + ".backup")
            _save_bytes_locked(backup_path, path.read_bytes())
        _save_locked(path, payload, expected_version=version)
        return payload


def _save_bytes_locked(path, data):
    fd, name = tempfile.mkstemp(prefix=".google-oauth-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def refresh_session(path):
    """Reread, refresh and commit under one lock; reuse a preceding winner.

    The save callable also discards a result if a nonparticipating writer
    replaced the token during refresh. Callback contention stays fail-closed.
    """
    path = Path(path)
    with writer_lock(path.parent, wait=True):
        previous = json.loads(path.read_text(encoding="utf-8"))

        def save(payload):
            version = file_version(path)
            current = json.loads(path.read_text(encoding="utf-8"))
            if current != previous:
                return current
            _save_locked(path, payload, expected_version=version)
            return payload

        yield previous, save


def restore_backup(path):
    """Explicit restore under the same lock; never creates a second backup."""
    path = Path(path)
    with writer_lock(path.parent):
        data = path.with_name(path.name + ".backup").read_bytes()
        json.loads(data)
        _save_bytes_locked(path, data)

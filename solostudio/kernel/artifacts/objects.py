from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from solostudio.kernel.errors import ArtifactDigestMismatch, MissingRetainedArtifact


@dataclass(frozen=True, slots=True)
class ObjectRecord:
    digest_sha256: str
    byte_size: int
    object_relpath: str


class ObjectStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.root = data_dir / "objects" / "sha256"
        self.tmp_root = data_dir / "object-tmp"
        self.root.mkdir(parents=True, exist_ok=True)
        self.tmp_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def promote_bytes(self, payload: bytes) -> ObjectRecord:
        digest = hashlib.sha256(payload).hexdigest()
        size = len(payload)
        relpath = self.relpath_for(digest)
        final_path = self.data_dir / relpath
        with self._lock:
            if final_path.exists():
                self._verify_path(final_path, digest, size)
                return ObjectRecord(digest, size, relpath)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f"{digest}.", suffix=".partial", dir=self.tmp_root)
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(tmp_path, final_path)
                    tmp_path.unlink(missing_ok=True)
                    self._fsync_directory(final_path.parent)
                except FileExistsError:
                    self._verify_path(final_path, digest, size)
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    if final_path.exists():
                        self._verify_path(final_path, digest, size)
                        tmp_path.unlink(missing_ok=True)
                    else:
                        os.replace(tmp_path, final_path)
                        self._fsync_directory(final_path.parent)
                self._verify_path(final_path, digest, size)
            finally:
                tmp_path.unlink(missing_ok=True)
        return ObjectRecord(digest, size, relpath)

    def verify(self, digest_sha256: str, byte_size: int, object_relpath: str | None = None) -> ObjectRecord:
        relpath = object_relpath or self.relpath_for(digest_sha256)
        path = self.data_dir / relpath
        self._verify_path(path, digest_sha256, byte_size)
        return ObjectRecord(digest_sha256, byte_size, relpath)

    def read_bytes(self, digest_sha256: str, byte_size: int, object_relpath: str | None = None) -> bytes:
        record = self.verify(digest_sha256, byte_size, object_relpath)
        return (self.data_dir / record.object_relpath).read_bytes()

    @staticmethod
    def relpath_for(digest_sha256: str) -> str:
        if len(digest_sha256) != 64 or any(c not in "0123456789abcdef" for c in digest_sha256):
            raise ValueError("invalid sha256 digest")
        return f"objects/sha256/{digest_sha256[:2]}/{digest_sha256[2:4]}/{digest_sha256}"

    @staticmethod
    def _verify_path(path: Path, expected_digest: str, expected_size: int) -> None:
        if not path.is_file():
            raise MissingRetainedArtifact(f"retained object is missing: {expected_digest}")
        actual_size = path.stat().st_size
        if actual_size != expected_size:
            raise ArtifactDigestMismatch(
                f"object size mismatch for {expected_digest}: expected {expected_size}, got {actual_size}"
            )
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        actual_digest = digest.hexdigest()
        if actual_digest != expected_digest:
            raise ArtifactDigestMismatch(
                f"object digest mismatch: expected {expected_digest}, got {actual_digest}"
            )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
        try:
            fd = os.open(path, flags)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

"""User-scoped DPAPI credential records under an inspected private ACL."""
import json
import os
from pathlib import Path
import re
import secrets
from dataclasses import dataclass
from typing import Callable
from . import _windows


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class CredentialCreationReceipt:
    reference: str
    file_identity: tuple[int, int]
    directory_identity: tuple[int, int]


class CredentialStore:
    def __init__(self, profile_dir: Path, namespace: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", namespace):
            raise CredentialError("invalid_credential_namespace")
        self.namespace = namespace
        try:
            parent = _windows.private_directory(profile_dir)
            self.directory = _windows.private_directory(parent / "credentials")
        except _windows.WindowsError:
            raise CredentialError("credential_protection_failed") from None

    def _path(self, reference):
        if not isinstance(reference, str) or not re.fullmatch(r"dpapi-v1-[0-9a-f]{64}", reference):
            raise CredentialError("invalid_credential_reference")
        return self.directory / (reference + ".dpapi")

    def create(self, profile_id: str, *,
               on_created: Callable[[CredentialCreationReceipt], None] | None = None) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", profile_id):
            raise CredentialError("invalid_credential_profile")
        reference = "dpapi-v1-" + secrets.token_hex(32)
        password = secrets.token_urlsafe(48)
        payload = {"version": 1, "namespace": self.namespace, "profile_id": profile_id,
                   "reference": reference, "secret": password}
        created = directory_identity = None
        try:
            _windows.assert_private(self.directory)
            directory_identity = _windows._file_identity(self.directory)
            protected = _windows.crypt(json.dumps(payload).encode("utf-8"))
            with self._path(reference).open("xb") as stream:
                stat = os.fstat(stream.fileno())
                created = (stat.st_dev, stat.st_ino)
                if on_created is not None:
                    on_created(CredentialCreationReceipt(reference, created, directory_identity))
                stream.write(protected)
                stream.flush()
                os.fsync(stream.fileno())
            if self.resolve(reference) != password:
                raise CredentialError("credential_readback_failed")
            return reference
        except BaseException as failure:
            # create() can fail before returning its reference. Clean only the
            # exact object we created; an xb collision or substituted record is
            # never ours to delete. The private directory identity must match too.
            if created is not None:
                try:
                    _windows.assert_private(self.directory)
                    path = self._path(reference)
                    if (_windows._file_identity(self.directory) == directory_identity
                            and _windows._file_identity(path) == created):
                        path.unlink()
                except (OSError, _windows.WindowsError):
                    pass
            if isinstance(failure, (OSError, _windows.WindowsError)):
                raise CredentialError("credential_persistence_failed") from None
            raise

    def _remove_created(self, receipt: CredentialCreationReceipt) -> None:
        """Remove only the object identified by the original exclusive-create FD."""
        try:
            _windows.assert_private(self.directory)
            if _windows._file_identity(self.directory) != receipt.directory_identity:
                raise CredentialError('credential_object_changed')
            path = self._path(receipt.reference)
            if not path.exists():
                return
            if _windows._file_identity(path) != receipt.file_identity:
                raise CredentialError('credential_object_changed')
            self.remove(receipt.reference)
        except (OSError, _windows.WindowsError):
            raise CredentialError('credential_removal_failed') from None

    def resolve(self, reference: str) -> str:
        path = self._path(reference)
        try:
            _windows.assert_private(path)
            payload = json.loads(_windows.crypt(path.read_bytes(), decrypt=True))
            if (payload["version"] != 1 or payload["namespace"] != self.namespace
                    or payload["reference"] != reference
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", payload["profile_id"])
                    or not re.fullmatch(r"[A-Za-z0-9_-]{64}", payload["secret"])):
                raise ValueError()
            return payload["secret"]
        except (OSError, ValueError, KeyError, TypeError, _windows.WindowsError):
            raise CredentialError("credential_unavailable") from None

    def remove(self, reference: str) -> None:
        path = self._path(reference)
        try:
            if path.exists():
                _windows.assert_private(path)
                path.unlink()
        except (OSError, _windows.WindowsError):
            raise CredentialError("credential_removal_failed") from None

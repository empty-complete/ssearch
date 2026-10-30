"""Windows CNG ECDSA P-256/SHA-256. No crypto fallback or private key support."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import os
import struct

from .contracts import UpdateError


def validate_public_blob(blob: bytes) -> None:
    # BCRYPT_ECCKEY_BLOB + uncompressed big-endian X and Y coordinates.
    if type(blob) is not bytes or len(blob) != 72 or struct.unpack('<II', blob[:8]) != (0x31534345, 32):
        raise UpdateError('update_key')


class CngVerifier:
    def verify(self, public_key: bytes, payload: bytes, signature: bytes) -> None:
        validate_public_blob(public_key)
        if type(payload) is not bytes or type(signature) is not bytes or len(signature) != 64:
            raise UpdateError('update_signature')
        if os.name != 'nt':
            raise UpdateError('update_platform')
        algorithm, key = wintypes.HANDLE(), wintypes.HANDLE()
        try:
            dll = ctypes.WinDLL('bcrypt.dll')
            handle, ulong, pointer = wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p
            declarations = {
                'BCryptOpenAlgorithmProvider': [ctypes.POINTER(handle), wintypes.LPCWSTR, wintypes.LPCWSTR, ulong],
                'BCryptImportKeyPair': [handle, handle, wintypes.LPCWSTR, ctypes.POINTER(handle), pointer, ulong, ulong],
                'BCryptVerifySignature': [handle, pointer, pointer, ulong, pointer, ulong, ulong],
                'BCryptDestroyKey': [handle],
                'BCryptCloseAlgorithmProvider': [handle, ulong],
            }
            for name, args in declarations.items():
                function = getattr(dll, name)
                function.argtypes, function.restype = args, ctypes.c_long
            if dll.BCryptOpenAlgorithmProvider(ctypes.byref(algorithm), 'ECDSA_P256', None, 0):
                raise UpdateError('update_signature')
            if dll.BCryptImportKeyPair(algorithm, None, 'ECCPUBLICBLOB', ctypes.byref(key),
                                      public_key, len(public_key), 0):
                raise UpdateError('update_key')
            digest = hashlib.sha256(payload).digest()
            if dll.BCryptVerifySignature(key, None, digest, len(digest), signature, len(signature), 0):
                raise UpdateError('update_signature')
        except (OSError, AttributeError, TypeError, ValueError):
            raise UpdateError('update_signature') from None
        finally:
            if key:
                dll.BCryptDestroyKey(key)
            if algorithm:
                dll.BCryptCloseAlgorithmProvider(algorithm, 0)

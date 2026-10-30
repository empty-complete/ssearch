"""Ephemeral Windows CNG signing keys, used only by update contract tests."""
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import sys


class TestSigningKey:
    __test__ = False

    def __init__(self):
        assert sys.platform == 'win32', 'the required CNG proof must run on Windows'
        self.dll = ctypes.WinDLL('bcrypt.dll')
        self.algorithm = wintypes.HANDLE()
        self.key = wintypes.HANDLE()
        self._configure()
        self._check(self.dll.BCryptOpenAlgorithmProvider(
            ctypes.byref(self.algorithm), 'ECDSA_P256', None, 0))
        self._check(self.dll.BCryptGenerateKeyPair(
            self.algorithm, ctypes.byref(self.key), 256, 0))
        self._check(self.dll.BCryptFinalizeKeyPair(self.key, 0))
        count = wintypes.ULONG()
        self._check(self.dll.BCryptExportKey(
            self.key, None, 'ECCPUBLICBLOB', None, 0, ctypes.byref(count), 0))
        blob = ctypes.create_string_buffer(count.value)
        self._check(self.dll.BCryptExportKey(
            self.key, None, 'ECCPUBLICBLOB', blob, len(blob), ctypes.byref(count), 0))
        self.public_blob = blob.raw[:count.value]

    def _configure(self):
        handle, ulong, pointer = wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p
        signatures = {
            'BCryptOpenAlgorithmProvider': [ctypes.POINTER(handle), wintypes.LPCWSTR, wintypes.LPCWSTR, ulong],
            'BCryptGenerateKeyPair': [handle, ctypes.POINTER(handle), ulong, ulong],
            'BCryptFinalizeKeyPair': [handle, ulong],
            'BCryptExportKey': [handle, handle, wintypes.LPCWSTR, pointer, ulong, ctypes.POINTER(ulong), ulong],
            'BCryptSignHash': [handle, pointer, pointer, ulong, pointer, ulong, ctypes.POINTER(ulong), ulong],
            'BCryptDestroyKey': [handle],
            'BCryptCloseAlgorithmProvider': [handle, ulong],
        }
        for name, args in signatures.items():
            function = getattr(self.dll, name)
            function.argtypes = args
            function.restype = ctypes.c_long

    @staticmethod
    def _check(status):
        assert status == 0, f'CNG test setup failed: {status & 0xffffffff:08x}'

    def sign(self, payload):
        digest = hashlib.sha256(payload).digest()
        signature = ctypes.create_string_buffer(64)
        count = wintypes.ULONG()
        self._check(self.dll.BCryptSignHash(
            self.key, None, digest, len(digest), signature, 64, ctypes.byref(count), 0))
        assert count.value == 64
        return signature.raw

    def trust_document(self, **changes):
        key = dict(key_id='fixture-p256', algorithm='ECDSA_P256_SHA256',
                   public_key_b64=base64.b64encode(self.public_blob).decode('ascii'),
                   not_before='2020-01-01T00:00:00Z', not_after='2100-01-01T00:00:00Z')
        key.update(changes)
        return {'format_version': 1, 'keys': [key]}

    def envelope(self, payload, *, key_id='fixture-p256', signature=None):
        return json.dumps(dict(key_id=key_id,
                               payload_b64=base64.b64encode(payload).decode('ascii'),
                               signature_b64=base64.b64encode(
                                   self.sign(payload) if signature is None else signature).decode('ascii')),
                          separators=(',', ':')).encode('utf-8')

    def close(self):
        if self.key:
            self.dll.BCryptDestroyKey(self.key)
            self.key = None
        if self.algorithm:
            self.dll.BCryptCloseAlgorithmProvider(self.algorithm, 0)
            self.algorithm = None

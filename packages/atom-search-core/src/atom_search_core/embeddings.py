"""Strictly local E5 inference, independent of the desktop shell and network clients."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

import numpy as np


class ModelUnavailable(RuntimeError):
    """The local model is absent, damaged, incompatible, or cannot encode safely."""


def _manifest_and_assets(model_dir: Path) -> dict:
    manifest = json.loads((model_dir / "manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != 1 or manifest.get("dimension") != 384
            or manifest.get("max_tokens") != 512
            or not isinstance(manifest.get("model_id"), str)
            or not manifest["model_id"]
            or not re.fullmatch(r"[0-9a-f]{40}", manifest.get("revision", ""))):
        raise ValueError("Unsupported local model manifest")
    assets = manifest.get("files")
    if not isinstance(assets, list) or len(assets) != 2:
        raise ValueError("The local model must have two pinned assets")
    required = {manifest.get("onnx_file"), manifest.get("tokenizer_file")}
    if required != {"model_quantized.onnx", "tokenizer.json"}:
        raise ValueError("Unexpected local model filenames")
    seen = set()
    for asset in assets:
        name = asset["path"]
        if name not in required or name in seen:
            raise ValueError("Unexpected or duplicate model asset")
        seen.add(name)
        path = model_dir / name
        if not path.resolve().is_relative_to(model_dir):
            raise ValueError("Model asset leaves the local model directory")
        size, expected_hash = asset["size"], asset["sha256"]
        if (isinstance(size, bool) or not isinstance(size, int) or size <= 0
                or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
            raise ValueError("Invalid pinned asset size or checksum")
        if path.stat().st_size != size:
            raise ValueError(f"Local model asset has the wrong size: {name}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected_hash:
            raise ValueError(f"Local model asset checksum mismatch: {name}")
    return manifest


class OnnxEmbedder:
    """An immutable local model session; tokenization is serialized across callers.

    Construction verifies the installed model once. Reuse this object rather than
    creating it per query. No model acquisition or network fallback occurs here.
    """

    dimension = 384

    def __init__(self, model_dir: Path, *, preparer=None):
        try:
            self.model_dir = Path(model_dir).resolve()
            manifest = _manifest_and_assets(self.model_dir)
            import onnxruntime as ort
            from .tokenization import PinnedTokenizer

            disable_telemetry = getattr(ort, "disable_telemetry_events", None)
            if disable_telemetry is not None:
                disable_telemetry()
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.log_severity_level = 3
            self._session = ort.InferenceSession(
                str(self.model_dir / manifest["onnx_file"]), sess_options=options,
                providers=["CPUExecutionProvider"])
            self._inputs = self._session.get_inputs()
            if (not any(item.name == "input_ids" for item in self._inputs)
                    or any(item.name not in {"input_ids", "attention_mask", "token_type_ids"}
                           for item in self._inputs)):
                raise ValueError("Unsupported local ONNX input names")
            self._preparer = preparer if preparer is not None else PinnedTokenizer(self.model_dir)
            self._tokenization = (self._preparer if isinstance(self._preparer, PinnedTokenizer)
                                  else PinnedTokenizer(self.model_dir))
            if self._tokenization.manifest != manifest:
                raise ValueError('Tokenizer and model identities differ')
            self.model_id = manifest["model_id"]
        except Exception as exc:
            raise ModelUnavailable(f"Локальная модель недоступна: {exc}") from exc

    def encode(self, texts: list[str], *, kind: str = "passage") -> np.ndarray:
        if kind not in {"query", "passage"}:
            raise ValueError("Embedding kind must be 'query' or 'passage'")
        if not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
            raise TypeError("texts must be a list of strings")
        if not texts:
            return np.empty((0, 384), dtype=np.float32)
        batches = []
        try:
            # Keep worst-case activation memory bounded even during a full scan.
            for offset in range(0, len(texts), 32):
                prepared = [self._preparer.prepare_input(text, kind=kind).text
                            for text in texts[offset:offset + 32]]
                encoded = self._tokenization.encode_batch(prepared, kind=kind)
                values = {
                    "input_ids": np.asarray([item.ids for item in encoded], dtype=np.int64),
                    "attention_mask": np.asarray([item.attention_mask for item in encoded], dtype=np.int64),
                    "token_type_ids": np.asarray([item.type_ids for item in encoded], dtype=np.int64),
                }
                feeds = {}
                for item in self._inputs:
                    dtype = np.int32 if item.type == "tensor(int32)" else np.int64
                    feeds[item.name] = values[item.name].astype(dtype, copy=False)
                outputs = self._session.run(None, feeds)
                if not outputs:
                    raise ValueError("The local model returned no embeddings")
                hidden = np.asarray(outputs[0], dtype=np.float32)
                if hidden.ndim == 3:
                    if hidden.shape[:2] != values["attention_mask"].shape or hidden.shape[2] != 384:
                        raise ValueError("Unexpected token embedding shape")
                    mask = values["attention_mask"].astype(np.float32)[..., None]
                    count = mask.sum(axis=1)
                    if np.any(count == 0):
                        raise ValueError("The local tokenizer returned an empty attention mask")
                    # where(), instead of multiplication, also excludes NaNs in padding.
                    pooled = np.where(mask.astype(bool), hidden, np.float32(0)).sum(axis=1) / count
                elif hidden.ndim == 2 and hidden.shape == (len(encoded), 384):
                    pooled = hidden
                else:
                    raise ValueError("Unexpected local embedding shape")
                norms = np.linalg.norm(pooled, axis=1, keepdims=True)
                if not np.isfinite(pooled).all() or not np.isfinite(norms).all() or np.any(norms <= 0):
                    raise ValueError("The local model returned non-finite or zero embeddings")
                batches.append((pooled / norms).astype(np.float32, copy=False))
        except ModelUnavailable:
            raise
        except Exception as exc:
            raise ModelUnavailable(f"Локальная модель не смогла выполнить поиск: {exc}") from exc
        return np.concatenate(batches, axis=0)

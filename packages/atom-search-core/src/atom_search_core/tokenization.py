"""Pinned E5 input preparation; importing this module never loads ONNX/numpy."""
import hashlib
import json
from pathlib import Path
import re
import threading

from .models import PreparedEmbeddingInput


def pinned_manifest(model_dir: Path, *, verify_onnx: bool = False) -> dict:
    directory = Path(model_dir).resolve()
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    if (type(manifest) is not dict or manifest.get('schema_version') != 1 or manifest.get('dimension') != 384
            or manifest.get('max_tokens') != 512 or not isinstance(manifest.get('model_id'), str)
            or not manifest['model_id'] or type(manifest.get('revision')) is not str
            or not re.fullmatch('[0-9a-f]{40}', manifest['revision'])
            or manifest.get('onnx_file') != 'model_quantized.onnx'
            or manifest.get('tokenizer_file') != 'tokenizer.json'):
        raise ValueError('Unsupported local model manifest')
    assets = manifest.get('files')
    if not isinstance(assets, list) or len(assets) != 2:
        raise ValueError('Expected two pinned model assets')
    seen = set()
    for asset in assets:
        if type(asset) is not dict:
            raise ValueError('Invalid pinned model asset')
        name, size, expected = asset.get('path'), asset.get('size'), asset.get('sha256')
        if (type(name) is not str or name not in {'model_quantized.onnx', 'tokenizer.json'} or name in seen
                or type(size) is not int or size <= 0 or not isinstance(expected, str)
                or not re.fullmatch('[0-9a-f]{64}', expected)):
            raise ValueError('Invalid pinned model asset')
        seen.add(name)
        path = directory / name
        if not path.resolve().is_relative_to(directory):
            raise ValueError('Model asset leaves local directory')
        if name == 'tokenizer.json' or verify_onnx:
            if path.stat().st_size != size:
                raise ValueError('Local model asset has wrong size: ' + name)
            with path.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != expected:
                raise ValueError('Local model asset checksum mismatch: ' + name)
    return manifest


class PinnedTokenizer:
    def __init__(self, model_dir: Path):
        self.manifest = pinned_manifest(model_dir)
        from tokenizers import Tokenizer
        self._tokenizer = Tokenizer.from_file(str(Path(model_dir) / 'tokenizer.json'))
        self._tokenizer.no_truncation()
        self._tokenizer.no_padding()
        self._lock = threading.RLock()

    def prepare_input(self, text: str, *, kind: str, max_tokens: int = 512) -> PreparedEmbeddingInput:
        if kind not in ('query', 'passage') or type(text) is not str:
            raise ValueError('invalid_embedding_input')
        if type(max_tokens) is not int or not 1 <= max_tokens <= 512:
            raise ValueError('invalid_token_budget')
        prefix = kind + ': '
        with self._lock:
            encoded = self._tokenizer.encode(prefix + text)
            if len(encoded.ids) > max_tokens:
                if len(self._tokenizer.encode(prefix).ids) > max_tokens:
                    raise ValueError('token_budget_smaller_than_prefix')
                ends = sorted({end - len(prefix) for _, end in encoded.offsets[:max_tokens]
                               if end > len(prefix)})
                while ends:
                    bounded = text[:ends.pop()]
                    counted = self._tokenizer.encode(prefix + bounded)
                    if len(counted.ids) <= max_tokens:
                        text, encoded = bounded, counted
                        break
                else:
                    text, encoded = '', self._tokenizer.encode(prefix)
            return PreparedEmbeddingInput(text, hashlib.sha256(text.encode('utf-8')).hexdigest(),
                                          len(encoded.ids))

    def encode_batch(self, texts: list[str], *, kind: str):
        """Tensor inputs use the same tokenizer, never a second silent truncation."""
        with self._lock:
            pad_id = self._tokenizer.token_to_id('<pad>')
            if pad_id is None:
                raise ValueError('Local tokenizer has no <pad> token')
            self._tokenizer.enable_padding(direction='right', pad_id=pad_id, pad_token='<pad>')
            try:
                result = self._tokenizer.encode_batch([kind + ': ' + text for text in texts])
                if any(len(item.ids) > 512 for item in result):
                    raise ValueError('Prepared input exceeds the actual model budget')
                return result
            finally:
                self._tokenizer.no_padding()

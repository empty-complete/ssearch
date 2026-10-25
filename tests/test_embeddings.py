"""Offline encoder behavior; tiny tokenizer/hidden states replace the large weights."""
import hashlib
import importlib
import importlib.util
import io
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import numpy as np
import pytest
from tokenizers import Tokenizer, models, pre_tokenizers


ROOT = Path(__file__).resolve().parents[1]
REVISION = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"
REMOTE_BASE = f"https://huggingface.co/Xenova/multilingual-e5-small/resolve/{REVISION}/"


@pytest.fixture
def model_directory(tmp_path):
    tokenizer = Tokenizer(models.WordLevel(
        {"[UNK]": 0, "query": 1, ":": 2, "passage": 3,
         "alpha": 4, "beta": 5, "gamma": 6, "<pad>": 7}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.save(str(tmp_path / "tokenizer.json"))
    (tmp_path / "model_quantized.onnx").write_bytes(b"tiny test model")
    files = []
    for local_name, remote_name in (("model_quantized.onnx", "onnx/model_quantized.onnx"),
                                    ("tokenizer.json", "tokenizer.json")):
        body = (tmp_path / local_name).read_bytes()
        files.append({"path": local_name, "url": REMOTE_BASE + remote_name,
                      "size": len(body), "sha256": hashlib.sha256(body).hexdigest()})
    manifest = {"schema_version": 1, "repository": "Xenova/multilingual-e5-small",
                "revision": REVISION, "model_id": "test-encoder@" + REVISION,
                "dimension": 384, "max_tokens": 512,
                "onnx_file": "model_quantized.onnx", "tokenizer_file": "tokenizer.json",
                "files": files}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


@pytest.fixture
def fake_inference(monkeypatch):
    """Keep real tokenization; deterministic hidden states make pooling observable."""
    import onnxruntime

    class Session:
        def get_inputs(self):
            return [SimpleNamespace(name=name, type="tensor(int64)")
                    for name in ("input_ids", "attention_mask", "token_type_ids")]

        def run(self, output_names, feeds):
            ids = feeds["input_ids"]
            if ids.shape[1] > 512:
                raise ValueError("encoder received more than 512 tokens")
            hidden = np.zeros((*ids.shape, 384), dtype=np.float32)
            hidden[:, :, 0] = ids
            hidden[:, :, 1] = 1
            return [hidden]

    def create_session(*args, **kwargs):
        options = kwargs["sess_options"]
        if kwargs["providers"] != ["CPUExecutionProvider"] or options.intra_op_num_threads != 2:
            raise ValueError("the local CPU/thread contract was not applied")
        return Session()

    monkeypatch.setattr(onnxruntime, "InferenceSession", create_session)
    return onnxruntime


def encoder_type():
    return importlib.import_module("atom_search_core.embeddings")


def test_missing_local_model_is_safe(tmp_path):
    module = encoder_type()
    with pytest.raises(module.ModelUnavailable):
        module.OnnxEmbedder(tmp_path)


def test_prefix_changes_query_embedding_numerically(model_directory, fake_inference):
    encoder = encoder_type().OnnxEmbedder(model_directory)
    query = encoder.encode(["alpha"], kind="query")
    passage = encoder.encode(["alpha"], kind="passage")
    np.testing.assert_allclose(query[0, :2], [0.91914503, 0.39391930], atol=1e-6)
    np.testing.assert_allclose(passage[0, :2], [0.94868330, 0.31622777], atol=1e-6)
    assert query.dtype == passage.dtype == np.float32
    assert query.shape == passage.shape == (1, 384)


def test_padding_does_not_change_shorter_item_embedding(model_directory, fake_inference):
    encoder = encoder_type().OnnxEmbedder(model_directory)
    batch = encoder.encode(["alpha", "alpha beta"])
    np.testing.assert_allclose(batch[0, :2], [0.94868330, 0.31622777], atol=1e-6)
    np.testing.assert_allclose(batch[1, :2], [0.96152395, 0.27472113], atol=1e-6)
    np.testing.assert_allclose(np.linalg.norm(batch, axis=1), [1, 1], atol=1e-6)
    np.testing.assert_array_equal(batch[:, 2:], np.zeros((2, 382), dtype=np.float32))


def test_already_pooled_embeddings_are_normalized(model_directory, fake_inference, monkeypatch):
    class Session:
        def get_inputs(self):
            return [SimpleNamespace(name="input_ids", type="tensor(int64)")]

        def run(self, output_names, feeds):
            values = np.zeros((len(feeds["input_ids"]), 384), dtype=np.float64)
            values[:, :2] = [3, 4]
            return [values]

    monkeypatch.setattr(fake_inference, "InferenceSession", lambda *args, **kwargs: Session())
    values = encoder_type().OnnxEmbedder(model_directory).encode(["alpha"])
    np.testing.assert_allclose(values[0, :2], [0.6, 0.8], atol=1e-6)
    assert values.dtype == np.float32


def test_empty_batch_is_typed_and_does_not_call_inference(model_directory, fake_inference, monkeypatch):
    class Session:
        def get_inputs(self):
            return [SimpleNamespace(name="input_ids", type="tensor(int64)")]

        def run(self, output_names, feeds):
            raise AssertionError("an empty batch must not reach inference")

    monkeypatch.setattr(fake_inference, "InferenceSession", lambda *args, **kwargs: Session())
    values = encoder_type().OnnxEmbedder(model_directory).encode([])
    assert values.shape == (0, 384)
    assert values.dtype == np.float32


def test_long_inputs_are_truncated_before_inference(model_directory, fake_inference):
    values = encoder_type().OnnxEmbedder(model_directory).encode(["alpha " * 1000])
    assert values.shape == (1, 384)
    np.testing.assert_allclose(np.linalg.norm(values, axis=1), [1], atol=1e-6)


@pytest.mark.parametrize("filename", ["model_quantized.onnx", "tokenizer.json"])
def test_corrupt_model_asset_is_rejected(model_directory, filename):
    module = encoder_type()
    path = model_directory / filename
    contents = bytearray(path.read_bytes())
    contents[-1] ^= 1
    path.write_bytes(contents)
    with pytest.raises(module.ModelUnavailable):
        module.OnnxEmbedder(model_directory)


def test_manifest_cannot_escape_model_directory(model_directory):
    module = encoder_type()
    path = model_directory / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../outside.onnx"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(module.ModelUnavailable):
        module.OnnxEmbedder(model_directory)


def test_runtime_encodes_when_sockets_are_blocked(model_directory, fake_inference, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("runtime attempted network access")

    monkeypatch.setattr(socket, "socket", no_network)
    values = encoder_type().OnnxEmbedder(model_directory).encode(["alpha"], kind="query")
    np.testing.assert_allclose(values[0, :2], [0.91914503, 0.39391930], atol=1e-6)


def test_unknown_embedding_kind_is_rejected(model_directory, fake_inference):
    with pytest.raises(ValueError):
        encoder_type().OnnxEmbedder(model_directory).encode(["alpha"], kind="unknown")


def test_multiple_batches_preserve_order_and_all_items(model_directory, fake_inference):
    values = encoder_type().OnnxEmbedder(model_directory).encode(["alpha"] * 32 + ["beta"])
    assert values.shape == (33, 384)
    np.testing.assert_allclose(values[0, :2], [0.94868330, 0.31622777], atol=1e-6)
    np.testing.assert_allclose(values[-1, :2], [0.95782629, 0.28734789], atol=1e-6)


@pytest.mark.parametrize("invalid", ["zero", "nonfinite", "wrong-batch", "wrong-dimension"])
def test_invalid_numerical_outputs_fail_safely(model_directory, fake_inference, monkeypatch, invalid):
    class Session:
        def get_inputs(self):
            return [SimpleNamespace(name="input_ids", type="tensor(int64)")]

        def run(self, output_names, feeds):
            if invalid == "wrong-batch":
                return [np.ones((2, 384), dtype=np.float32)]
            if invalid == "wrong-dimension":
                return [np.ones((1, 383), dtype=np.float32)]
            return [np.full((1, 384), 0 if invalid == "zero" else np.nan, dtype=np.float32)]

    monkeypatch.setattr(fake_inference, "InferenceSession", lambda *args, **kwargs: Session())
    module = encoder_type()
    with pytest.raises(module.ModelUnavailable):
        module.OnnxEmbedder(model_directory).encode(["alpha"])


def load_fetch_tool():
    spec = importlib.util.spec_from_file_location("atom_fetch_model", ROOT / "tools" / "fetch_model.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fetch_preserves_previous_asset_on_hash_mismatch(model_directory, tmp_path, monkeypatch):
    tool = load_fetch_tool()
    destination = tmp_path / "downloaded"
    destination.mkdir()
    previous = destination / "model_quantized.onnx"
    previous.write_bytes(b"previous valid installation")
    manifest = model_directory / "manifest.json"
    monkeypatch.setattr(tool, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(tool.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"bad"))
    with pytest.raises(ValueError):
        tool.fetch_model(destination)
    assert previous.read_bytes() == b"previous valid installation"
    assert sorted(path.name for path in destination.iterdir()) == ["model_quantized.onnx"]


def test_fetch_is_idempotent_without_network_when_assets_valid(model_directory, monkeypatch):
    tool = load_fetch_tool()
    monkeypatch.setattr(tool, "MANIFEST_PATH", model_directory / "manifest.json")

    def no_network(*args, **kwargs):
        raise AssertionError("valid local assets must not be downloaded again")

    monkeypatch.setattr(tool.urllib.request, "urlopen", no_network)
    tool.fetch_model(model_directory)
    tool.fetch_model(model_directory)
    assert (model_directory / "model_quantized.onnx").read_bytes() == b"tiny test model"


def test_fetch_writes_verified_assets_and_manifest_atomically(model_directory, tmp_path, monkeypatch):
    tool = load_fetch_tool()
    manifest_path = model_directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bodies = {item["url"]: (model_directory / item["path"]).read_bytes() for item in manifest["files"]}
    destination = tmp_path / "downloaded"
    monkeypatch.setattr(tool, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(tool.urllib.request, "urlopen", lambda request, **kwargs: io.BytesIO(bodies[request.full_url]))
    tool.fetch_model(destination)
    assert (destination / "model_quantized.onnx").read_bytes() == b"tiny test model"
    assert (destination / "tokenizer.json").read_bytes() == (model_directory / "tokenizer.json").read_bytes()
    assert json.loads((destination / "manifest.json").read_text(encoding="utf-8")) == manifest
    assert len(list(destination.iterdir())) == 3


def test_fetch_refuses_unpinned_external_url(model_directory, tmp_path, monkeypatch):
    tool = load_fetch_tool()
    path = model_directory / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"][0]["url"] = "https://example.com/model.onnx"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(tool, "MANIFEST_PATH", path)
    with pytest.raises(ValueError):
        tool.fetch_model(tmp_path / "downloaded")


@pytest.mark.skipif(not all((ROOT / "models" / name).is_file()
                           for name in ("model_quantized.onnx", "tokenizer.json")),
                    reason="optional: both bundled model assets have not been acquired")
def test_real_model_offline_smoke(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("local model attempted network access")

    monkeypatch.setattr(socket, "socket", no_network)
    encoder = encoder_type().OnnxEmbedder(ROOT / "models")
    values = encoder.encode(["чертежи монтажного узла", "отчёт о командировке"])
    assert values.shape == (2, 384)
    assert values.dtype == np.float32
    assert np.isfinite(values).all()
    np.testing.assert_allclose(np.linalg.norm(values, axis=1), [1, 1], atol=1e-5)

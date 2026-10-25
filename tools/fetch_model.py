"""Build-time acquisition of two pinned public model assets; never used by the app."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.request


MANIFEST_PATH = Path(__file__).resolve().parents[1] / "models" / "manifest.json"
REVISION = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"
BASE_URL = f"https://huggingface.co/Xenova/multilingual-e5-small/resolve/{REVISION}/"
ALLOWED_URLS = {"model_quantized.onnx": BASE_URL + "onnx/model_quantized.onnx",
                "tokenizer.json": BASE_URL + "tokenizer.json"}


def _verified(path: Path, asset: dict) -> bool:
    try:
        if path.stat().st_size != asset["size"]:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest() == asset["sha256"]
    except OSError:
        return False


def _read_manifest() -> tuple[dict, bytes]:
    raw = MANIFEST_PATH.read_bytes()
    manifest = json.loads(raw)
    if (manifest.get("schema_version") != 1
            or manifest.get("repository") != "Xenova/multilingual-e5-small"
            or manifest.get("revision") != REVISION
            or manifest.get("dimension") != 384
            or manifest.get("max_tokens") != 512):
        raise ValueError("Unexpected pinned model manifest")
    assets = manifest.get("files")
    if not isinstance(assets, list) or len(assets) != 2:
        raise ValueError("Expected exactly the two pinned model assets")
    seen = set()
    for asset in assets:
        name = asset.get("path")
        if (name not in ALLOWED_URLS or name in seen
                or asset.get("url") != ALLOWED_URLS[name]
                or not isinstance(asset.get("size"), int) or isinstance(asset["size"], bool)
                or asset["size"] <= 0
                or not re.fullmatch(r"[0-9a-f]{64}", asset.get("sha256", ""))):
            raise ValueError("Manifest contains an unpinned model asset")
        seen.add(name)
    return manifest, raw


def _download_asset(directory: Path, asset: dict) -> None:
    target = directory / asset["path"]
    if not target.resolve().is_relative_to(directory):
        raise ValueError("Model destination leaves the selected directory")
    if _verified(target, asset):
        print(f"Verified locally: {asset['path']}")
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".model-", suffix=".tmp", dir=directory,
                                         delete=False) as output:
            temporary = Path(output.name)
            request = urllib.request.Request(asset["url"], headers={"User-Agent": "ssearch-build/0.2.0"})
            digest = hashlib.sha256()
            size = 0
            with urllib.request.urlopen(request, timeout=60) as source:
                while block := source.read(1024 * 1024):
                    size += len(block)
                    if size > asset["size"]:
                        raise ValueError(f"Downloaded asset is larger than pinned: {asset['path']}")
                    digest.update(block)
                    output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if size != asset["size"] or digest.hexdigest() != asset["sha256"]:
            raise ValueError(f"Downloaded asset does not match its checksum: {asset['path']}")
        os.replace(temporary, target)
        temporary = None
        print(f"Downloaded and verified: {asset['path']} ({size} bytes)")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def fetch_model(model_dir: Path) -> dict:
    """Acquire known assets atomically per file, preserving valid previous data."""
    manifest, raw = _read_manifest()
    directory = Path(model_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    for asset in manifest["files"]:
        _download_asset(directory, asset)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".manifest-", suffix=".tmp", dir=directory,
                                         delete=False) as output:
            temporary = Path(output.name)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / "manifest.json")
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=MANIFEST_PATH.parent,
                        help="Destination for verified model files (default: repository models/)")
    args = parser.parse_args(argv)
    try:
        manifest = fetch_model(args.model_dir)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"Model acquisition failed: {exc}\n")
    print(f"Ready for offline packaging: {manifest['model_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

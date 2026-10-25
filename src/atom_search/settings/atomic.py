"""Replace JSON only after its complete UTF-8 document is flushed to disk."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_create_bytes(target: Path, value: bytes) -> None:
    """Publish a complete same-directory raw backup without replacing a file."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', dir=target.parent,
                                         prefix=f'.{target.name}.', suffix='.tmp',
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == 'nt':
            # Windows rename is atomic and refuses an existing destination.
            os.rename(temporary, target)
            temporary = None
        else:
            # POSIX rename replaces existing files; link preserves create-only.
            os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_write_json(target: Path, value: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=target.parent, prefix=f'.{target.name}.',
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

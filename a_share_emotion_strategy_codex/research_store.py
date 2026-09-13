"""Private runtime storage, separate from the distributable research tools."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parent


def private_root() -> Path:
    override = os.environ.get('AUTOSTRATEGY_PRIVATE_ROOT')
    return Path(override).expanduser() if override else Path.home() / 'Library/Application Support/AutoStrategy/workbench'


def private_path(relative: str = '') -> Path:
    part = Path(relative)
    if part.is_absolute() or '..' in part.parts:
        raise ValueError('Only relative private paths are accepted')
    return private_root() / part


def research_path(relative: str = '') -> Path:
    return private_path('research') / relative


def config_path(name: str) -> Path:
    if Path(name).name != name:
        raise ValueError('Invalid configuration name')
    local = private_path('config') / name
    return local if local.exists() else ROOT / 'examples/config' / name


def load_config(name: str) -> dict:
    return json.loads(config_path(name).read_text(encoding='utf-8'))


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, ensure_ascii=False, indent=2, allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def update_lock():
    path = private_path('dashboard.lock')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def semantic_hash(value) -> str:
    """Ignore build clocks, not source timestamps or changed evidence."""
    def clean(item):
        if isinstance(item, dict):
            return {k: clean(v) for k, v in item.items() if k not in ('published_at', 'built_at')}
        if isinstance(item, list):
            return [clean(x) for x in item]
        return item
    return hashlib.sha256(json.dumps(clean(value), ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':'), allow_nan=False).encode()).hexdigest()

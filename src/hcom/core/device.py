"""Device identity management"""
from __future__ import annotations
import uuid
from .paths import hcom_path, atomic_write
from .instances import hash_to_name


def get_device_uuid() -> str:
    """Get or create persistent device UUID."""
    device_file = hcom_path('.tmp', 'device_id')
    if device_file.exists():
        return device_file.read_text().strip()
    device_id = str(uuid.uuid4())
    device_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(device_file, device_id)
    return device_id


def get_device_short_id(device_id: str | None = None) -> str:
    """Get 4-char word-based device ID (e.g., 'BOXE')."""
    if device_id is None:
        device_id = get_device_uuid()
    return hash_to_name(device_id).upper()


def add_device_suffix(name: str | None, device_id: str) -> str | None:
    """Add :DEVICE suffix to instance name."""
    if not name:
        return None
    short_id = get_device_short_id(device_id)
    return name if ':' in name else f"{name}:{short_id}"


__all__ = [
    'get_device_uuid',
    'get_device_short_id',
    'add_device_suffix',
]

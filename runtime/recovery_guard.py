"""Fail closed while an assisted recovery awaits support release."""
import os
from pathlib import Path


def assert_operation_allowed():
    root = Path(os.environ.get("SYSVARHUB_PROGRAMDATA", r"C:\ProgramData\SysvarHub"))
    marker = root / "config" / "recovery-block.json"
    if marker.exists() or marker.is_symlink():
        raise RuntimeError("Hub bloqueado para recuperacao assistida; liberacao exclusiva do suporte.")

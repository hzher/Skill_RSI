"""Read CLI configuration without importing optional backends on startup."""
from pathlib import Path

from ..evidence.io import read_json


def load_config(path: Path) -> dict:
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise ValueError("YAML input requires skill-rsi[yaml]") from exc
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    else:
        value = read_json(path)
    if not isinstance(value, dict):
        raise ValueError("configuration must be an object")
    return value

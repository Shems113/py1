import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]

def _resolve_path(file_path: str | Path) -> Path:
    """Helper privé : convertit en Path et résout les chemins relatifs par rapport à la racine du projet."""
    path_obj = Path(file_path)
    return path_obj if path_obj.is_absolute() else PROJECT_ROOT / path_obj

def json_from_file(file_path: str | Path) -> dict[str, Any]:
    """Lit un fichier JSON."""
    target_path = _resolve_path(file_path)
    with open(target_path, encoding="utf-8") as f:
        return json.load(f)

def json_to_file(
    file_path: str | Path,
    data: dict[str, Any]) -> None:
    """Écrit un dictionnaire dans un fichier JSON."""
    target_path = _resolve_path(file_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def json_to_stdout(data: list[dict[str, Any]]) -> None:
    """Écrit un dictionnaire dans stdout en JSON"""
    json_bytes = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(json_bytes)
    sys.stdout.buffer.flush()


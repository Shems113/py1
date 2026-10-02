import datetime as dt
import json
import os
import sys
import tempfile
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from Common.Helpers.File import resolve_project_path


def _json_default(obj: Any)-> Any:
    """Sérialise les types courants que json ne gère pas nativement
    (numpy / pandas, Path, dates, Decimal, Enum, set…) au lieu de lever TypeError."""
    if hasattr(obj, "item") and callable(obj.item) and getattr(obj, "ndim", None) == 0:
        return obj.item()  # scalaires numpy (int64, float64, bool_)
    if hasattr(obj, "tolist") and callable(obj.tolist):
        return obj.tolist()  # tableaux numpy, Series pandas
    if isinstance(obj, (dt.datetime, dt.date, dt.time)):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return str(obj)  # Path, Timestamp, UUID…

def json_from_file(file_path: str | Path)-> dict[str, Any]:
    """Lit un fichier JSON (un BOM UTF-8 éventuel est accepté)."""
    target_path = resolve_project_path(file_path)
    with open(target_path, encoding="utf-8-sig") as f:
        return json.load(f)

def json_to_file(
    file_path: str | Path,
    data: dict[str, Any])-> None:
    """Écrit un dictionnaire dans un fichier JSON.

    Écriture atomique : fichier temporaire puis remplacement. Un arrêt en cours
    d'écriture (Ctrl+C, plantage) ne laisse jamais un JSON tronqué ou vide."""
    target_path = resolve_project_path(file_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target_path.name}.", suffix=".tmp",
                                    dir=target_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=_json_default)
        os.replace(tmp_name, target_path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise

def json_to_stdout(data: Any)-> None:
    """Écrit des données dans stdout en JSON (UTF-8)."""
    json_bytes = json.dumps(data, indent=2, ensure_ascii=False, default=_json_default).encode("utf-8")
    sys.stdout.buffer.write(json_bytes)
    sys.stdout.buffer.flush()

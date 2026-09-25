import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Formats Word COM (16: docx, 17: pdf)
WORD_FORMAT_EXTENSIONS = {
    16: None,  # Garde l'extension d'origine (.docx / .doc)
    17: ".pdf",
}

HASH_ALGOS = ["md5"]

def clean_directory_name(dir_name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', " ", dir_name).strip()

def clean_last_directory_name(dir_name: str) -> str:
    result = clean_directory_name(dir_name).strip()
    while result.endswith("."):
        result = result[:-1]
    return result

def clean_file_name(file_name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", file_name).strip()

def remove_empty_folders(path_directory: str | Path) -> None:
    """Parcourt et supprime récursivement les dossiers vides."""
    path_obj = Path(path_directory)
    if not path_obj.exists():
        return

    for root, dirs, files in os.walk(path_obj, topdown=False):
        if not (any(dirs) or any(files)):
            for f in files:
                os.remove(os.path.join(root, f))
            try:
                os.rmdir(root)
            except OSError:
                pass

def get_folder_name(file_path: str | Path) -> str:
    return str(Path(file_path).parent)

def get_folder_names(file_path: str | Path) -> str:
    return Path(file_path).parent.name

def get_file_name_with_extension(file_path: str | Path) -> str:
    return Path(file_path).name

def get_file_name_without_extension(file_path: str | Path) -> str:
    return Path(file_path).stem

def get_file_name_extension(file_path: str | Path) -> str:
    return Path(file_path).suffix.lower()

def get_target_filename_for_word(
    file_path: str | Path,
    file_format: int | None
) -> str | None:
    """Génère le nom de fichier cible selon le format Word exporté."""
    path_obj = Path(file_path)
    if file_format not in WORD_FORMAT_EXTENSIONS:
        return None

    ext = WORD_FORMAT_EXTENSIONS[file_format]
    return f"{path_obj.stem}{ext}" if ext else path_obj.name

def compute_file_hash(
    file_path: str | Path,
    algorithm: str = "sha256"
) -> str:
    """Calcule le hash d'un fichier."""
    hasher = hashlib.new(algorithm)
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()

def get_file_common_fields(
    file_path: str | Path,
    include_all_data: bool = False
) -> dict[str, Any]:
    """Extrait les métadonnées de base d'un fichier."""
    path_obj = Path(file_path)
    stats = path_obj.stat()

    result = {
        "extension": path_obj.suffix.lower(),
        "sizeInBytes": stats.st_size,
        "lastModified": time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(stats.st_mtime)
        ),
    }

    if include_all_data:
        result.update(
            {
                "folder": path_obj.parent.name,
                "name": path_obj.stem,
                **{
                    f"hash{algo.upper()}": compute_file_hash(path_obj, algo)
                    for algo in HASH_ALGOS
                },
            }
        )

    return {"path": str(path_obj)} | result

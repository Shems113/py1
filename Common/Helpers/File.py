import hashlib
import os
import re
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Formats Word COM (16: docx, 17: pdf)
WORD_FORMAT_EXTENSIONS = {
    16: None,  # Garde l'extension d'origine (.docx / .doc)
    17: ".pdf",
}

HASH_ALGOS = ["md5"]

# Caractères interdits dans un nom Windows, y compris les caractères de contrôle
_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_HASH_CHUNK_SIZE = 1024 * 1024  # 1 Mio : ~10x plus rapide que 8 Kio sur de gros fichiers


def clean_directory_name(dir_name: str)-> str:
    return _INVALID_NAME_CHARS.sub(" ", dir_name).strip()

def clean_last_directory_name(dir_name: str)-> str:
    # Windows refuse un nom de dossier terminé par un point OU un espace
    return clean_directory_name(dir_name).rstrip(". ")

def clean_file_name(file_name: str)-> str:
    # Windows refuse un nom de fichier terminé par un point ou un espace
    return _INVALID_NAME_CHARS.sub("_", file_name).strip().rstrip(".").rstrip()

def remove_empty_folders(path_directory: str | Path)-> None:
    """Supprime récursivement les dossiers vides, y compris ceux qui ne contenaient
    que des sous-dossiers vides (le dossier racine est supprimé s'il devient vide)."""
    path_obj = Path(path_directory)
    if not path_obj.is_dir():
        return

    # topdown=False : les enfants sont traités avant leur parent. La liste « dirs »
    # fournie par os.walk date d'avant la suppression des enfants : on re-vérifie
    # donc le contenu réel du dossier au moment de le traiter.
    for root, _dirs, _files in os.walk(path_obj, topdown=False):
        try:
            with os.scandir(root) as it:
                if next(it, None) is not None:
                    continue
            os.rmdir(root)
        except OSError:
            pass

def get_folder_name(file_path: str | Path)-> str:
    return str(Path(file_path).parent)

def get_folder_names(file_path: str | Path)-> str:
    return Path(file_path).parent.name

def get_file_name_with_extension(file_path: str | Path)-> str:
    return Path(file_path).name

def get_file_name_without_extension(file_path: str | Path)-> str:
    return Path(file_path).stem

def get_file_name_extension(file_path: str | Path)-> str:
    return Path(file_path).suffix.lower()

def get_target_filename_for_word(
    file_path: str | Path,
    file_format: int | None
)-> str | None:
    """Génère le nom de fichier cible selon le format Word exporté."""
    path_obj = Path(file_path)
    if file_format not in WORD_FORMAT_EXTENSIONS:
        return None

    ext = WORD_FORMAT_EXTENSIONS[file_format]
    return f"{path_obj.stem}{ext}" if ext else path_obj.name

def compute_file_hash(
    file_path: str | Path,
    algorithm: str = "sha256"
)-> str:
    """Calcule le hash d'un fichier."""
    with open(file_path, "rb") as f:
        return hashlib.file_digest(f, algorithm).hexdigest()

def compute_file_hashes(
    file_path: str | Path,
    algorithms: Iterable[str],
)-> dict[str, str]:
    """Calcule plusieurs hashes en UNE seule lecture du fichier."""
    hashers = {algo: hashlib.new(algo) for algo in algorithms}
    if not hashers:
        return {}
    with open(file_path, "rb") as f:
        while chunk := f.read(_HASH_CHUNK_SIZE):
            for hasher in hashers.values():
                hasher.update(chunk)
    return {algo: hasher.hexdigest() for algo, hasher in hashers.items()}

def get_file_common_fields(
    file_path: str | Path,
    include_all_data: bool = False
)-> dict[str, Any]:
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
        hashes = compute_file_hashes(path_obj, HASH_ALGOS)
        result.update(
            {
                "folder": path_obj.parent.name,
                "name": path_obj.stem,
                **{f"hash{algo.upper()}": digest for algo, digest in hashes.items()},
            }
        )

    return {"path": str(path_obj)} | result

def resolve_project_path(raw_path: str | Path | None)-> Path:
    """Chemin absolu tel quel ; chemin relatif (ou vide) résolu depuis la racine du projet."""
    if not raw_path:
        return PROJECT_ROOT
    path_obj = Path(raw_path)
    return path_obj if path_obj.is_absolute() else PROJECT_ROOT / path_obj

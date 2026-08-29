from datetime import datetime, timedelta
import hashlib
import json
import locale
import mimetypes
import numbers
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from functools import cache
import charset_normalizer
from ffmpeg._probe import probe as ffmpeg_probe
from mutagen.aac import AAC
from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC
from mutagen.id3 import APIC, ID3
from mutagen.m4a import M4A
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE
import numpy as np
import pandas as pd

# Mappings des types MIME
MIME_TYPE_TO_AUDIO = {
    "audio/aac": AAC,
    "audio/flac": FLAC,
    "audio/mpeg": MP3,
    "audio/ogg": OggVorbis,
    "audio/opus": OggVorbis,
    "audio/wav": WAVE,
    "audio/x-wav": WAVE,
    "audio/x-m4a": M4A,
}
MIME_TYPE_TO_VIDEO = {"video/mp4": MP4}
HASH_ALGOS = ["md5"]

# Formats Word COM (16: docx, 17: pdf)
WORD_FORMAT_EXTENSIONS = {
    16: None,  # Garde l'extension d'origine (.docx / .doc)
    17: ".pdf",
}

_FFMPEG_CHECKED = False
_DECIMAL_SEPARATOR: Optional[str] = None


# --- FFMPEG & System Utilities ---

@cache
def get_ffmpeg_version() -> str:
    """Retourne la première ligne de version de FFmpeg."""
    try:
        res = subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        return res.stdout.split("\n")[0]
    except Exception:
        return "ffmpeg introuvable dans le PATH"

@cache
def ensure_ffmpeg_available(verbose: bool = True) -> Optional[str]:
    """Vérifie la présence de ffmpeg."""
    global _FFMPEG_CHECKED
    ffmpeg_path = shutil.which("ffmpeg")
    if verbose:
        print("Lien de ffmpeg : ", ffmpeg_path)
        if ffmpeg_path:
            print("Version de ffmpeg : ", get_ffmpeg_version())
        else:
            print("Version de ffmpeg : ffmpeg introuvable dans le PATH")
    _FFMPEG_CHECKED = True
    return ffmpeg_path


def get_decimal_separator() -> str:
    """Initialise la locale et retourne le séparateur décimal (mis en cache)."""
    global _DECIMAL_SEPARATOR
    if _DECIMAL_SEPARATOR is None:
        try:
            locale.setlocale(locale.LC_ALL, "")
        except locale.Error:
            pass
        _DECIMAL_SEPARATOR = locale.localeconv().get("decimal_point", ".")
    return _DECIMAL_SEPARATOR


# --- Media Metadata & Probing ---


def get_audio_id3_tags(file_path: Union[str, Path]) -> Any:
    """Extrait les étiquettes ID3/EasyID3 d'un fichier audio."""
    path_str = str(file_path)
    mime = mimetypes.guess_type(path_str)[0]
    audio_cls = MIME_TYPE_TO_AUDIO.get(mime)
    return audio_cls(path_str, ID3=EasyID3) if audio_cls else None


def get_audio_tags(file_path: Union[str, Path]) -> Any:
    """Extrait les étiquettes de métadonnées vidéo/audio de base."""
    path_str = str(file_path)
    mime = mimetypes.guess_type(path_str)[0]
    video_cls = MIME_TYPE_TO_VIDEO.get(mime)
    return video_cls(path_str) if video_cls else None


def add_audio_id3_images(
    file_path: Union[str, Path], image_data_dico: Dict[Union[str, Path], Dict[int, str]]
) -> None:
    """Ajoute des pochettes (images APIC) dans un fichier MP3/ID3."""
    path_str = str(file_path)
    audio = ID3(path_str)

    # Nettoyage des couvertures existantes
    for tag in [t for t in audio.keys() if t.startswith("APIC")]:
        del audio[tag]

    for img_path, img_type_dico in image_data_dico.items():
        with open(img_path, "rb") as img:
            img_bytes = img.read()
            for img_type, img_desc in img_type_dico.items():
                apic = APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=img_type,
                    desc=img_desc,
                    data=img_bytes,
                )
                audio.add(apic)
    audio.save()


def get_video_streams(file_path: Union[str, Path]) -> List[Dict[str, Any]]:
    """Récupère les flux vidéo/audio via ffmpeg_probe."""
    if not _FFMPEG_CHECKED:
        ensure_ffmpeg_available()
    return ffmpeg_probe(str(file_path)).get("streams", [])


def get_video_chapters(file_path: Union[str, Path]) -> List[Dict[str, Any]]:
    """Récupère la liste des chapitres vidéo via ffmpeg_probe."""
    if not _FFMPEG_CHECKED:
        ensure_ffmpeg_available()
    return ffmpeg_probe(str(file_path), show_chapters=None).get("chapters", [])


def is_media_type(file_path: Union[str, Path], media_type: str) -> bool:
    """Vérifie si le type MIME commence par le préfixe spécifié (ex: 'audio', 'video')."""
    mime = mimetypes.guess_type(str(file_path))[0] or ""
    return mime.startswith(media_type)


def is_audio(file_path: Union[str, Path]) -> bool:
    return is_media_type(file_path, "audio")


def is_video(file_path: Union[str, Path]) -> bool:
    return is_media_type(file_path, "video")


def is_text_file(file_path: Union[str, Path]) -> bool:
    return is_media_type(file_path, "text")


def is_media(file_path: Union[str, Path]) -> bool:
    return is_audio(file_path) or is_video(file_path) or is_text_file(file_path)


# --- Directory & File Name Cleaning ---


def clean_directory_name(dir_name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', " ", dir_name).strip()


def clean_last_directory_name(dir_name: str) -> str:
    result = clean_directory_name(dir_name).strip()
    while result.endswith("."):
        result = result[:-1]
    return result


def clean_file_name(file_name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", file_name).strip()


# --- File Operations & Path Parsing ---


def move_media_file(
    source_path: Union[str, Path],
    directory_name: Union[str, Path],
    file_name_func: Callable[[int, str], str],
    is_same_media_func: Callable[[Union[str, Path]], bool],
    default_index: int = 0,
    is_move: bool = True,
) -> Tuple[str, bool]:
    """Déplace ou copie un fichier vers une destination en gérant les collisions."""
    src = Path(source_path)
    target_dir = Path(directory_name)
    separator = "_"
    index = default_index

    target_file_path = target_dir / file_name_func(0, separator)

    while target_file_path.is_file():
        if is_same_media_func(str(target_file_path)):
            return str(target_file_path), False
        index += 1
        target_file_path = target_dir / file_name_func(index, separator)

    target_file_path.parent.mkdir(parents=True, exist_ok=True)

    if is_move:
        shutil.move(src, target_file_path)
    else:
        shutil.copy(src, target_file_path)

    return str(target_file_path), True


def remove_empty_folders(path_directory: Union[str, Path]) -> None:
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


def get_folder_name(file_path: Union[str, Path]) -> str:
    return str(Path(file_path).parent)


def get_folder_names(file_path: Union[str, Path]) -> str:
    return Path(file_path).parent.name


def get_file_name_with_extension(file_path: Union[str, Path]) -> str:
    return Path(file_path).name


def get_file_name_without_extension(file_path: Union[str, Path]) -> str:
    return Path(file_path).stem


def get_file_name_extension(file_path: Union[str, Path]) -> str:
    return Path(file_path).suffix.lower()


def get_target_filename_for_word(
    file_path: Union[str, Path], file_format: Optional[int]
) -> Optional[str]:
    """Génère le nom de fichier cible selon le format Word exporté."""
    path_obj = Path(file_path)
    if file_format not in WORD_FORMAT_EXTENSIONS:
        return None

    ext = WORD_FORMAT_EXTENSIONS[file_format]
    return f"{path_obj.stem}{ext}" if ext else path_obj.name


# --- Hashing & Stats ---


def compute_file_hash(
    file_path: Union[str, Path], algorithm: str = "sha256"
) -> str:
    """Calcule le hash d'un fichier."""
    hasher = hashlib.new(algorithm)
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()


def get_file_common_fields(
    file_path: Union[str, Path], include_all_data: bool = False
) -> Dict[str, Any]:
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


# --- JSON & I/O ---


def json_from_file(file_path: Union[str, Path]) -> Dict[str, Any]:
    """Lit un fichier JSON."""
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def json_to_file(file_path: Union[str, Path], data: Dict[str, Any]) -> None:
    """Écrit un dictionnaire dans un fichier JSON."""
    path_obj = Path(file_path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    with open(path_obj, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# --- Pandas & Data Processing ---


def data_frame_from_csv(
    file_path: Union[str, Path], separator: str
) -> pd.DataFrame:
    """Lit un CSV avec détection d'encodage."""
    with open(file_path, "rb") as f:
        encoding = (
            charset_normalizer.detect(f.read(100_000)).get("encoding")
            or "utf-8"
        )
    return pd.read_csv(
        file_path,
        sep=separator,
        encoding=encoding,
        decimal=get_decimal_separator(),
    )


def data_frame_to_csv(
    df: pd.DataFrame,
    file_path: Union[str, Path],
    separator: str,
    fields: Optional[List[str]] = None,
) -> None:
    """Exporte un DataFrame vers un CSV."""
    path_obj = Path(file_path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)

    if fields:
        valid_fields = [col for col in fields if col in df.columns]
        df = df[valid_fields]

    df.to_csv(
        path_obj,
        sep=separator,
        encoding="utf-8-sig",
        decimal=get_decimal_separator(),
        index=False,
    )


def select_from_dico(dico: Dict[Any, Any], default_value: Any) -> np.ndarray:
    """Vectorisation np.select à partir d'un dictionnaire {valeur: condition}."""
    condlist = list(dico.values())
    choicelist = list(dico.keys())
    return np.select(condlist, choicelist, default=default_value)


def compute_carrefour_bill_values(
    df: pd.DataFrame,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Calcule le résumé de facturation Carrefour."""
    df_calc = df.assign(priceWithTva=df["price"] * df["tva"] / 100)
    bill_date = datetime.now() + timedelta(days=-1, hours=-5)
    total = df_calc["price"].sum()
    tva_20 = df_calc[df_calc["tva"] == 20]["price"].sum()
    tva_5_5 = df_calc[df_calc["tva"] == 5.5]["price"].sum()

    values = {
        "billdDateTime1": bill_date.strftime("%d/%m/%Y à %Hh%M"),
        "billdDateTime2": (bill_date + timedelta(minutes=2)).strftime(
            "%d.%m.%Y %H:%M"
        ),
        "total": total,
        "montantCBPayé": total,
        "totalTVA20": tva_20,
        "totalTVA5_5": tva_5_5,
        "totalTVA": tva_20 + tva_5_5,
        "tva": df_calc["priceWithTva"].sum(),
        "tva20": df_calc[df_calc["tva"] == 20]["priceWithTva"].sum(),
        "tva5_5": df_calc[df_calc["tva"] == 5.5]["priceWithTva"].sum(),
    }

    df_res = df_calc.drop(columns=["priceWithTva"]).assign(
        tva=df_calc["tva"].astype(str) + "%"
    )
    formatted = {
        k: f"{round(v, 2)}€" if isinstance(v, numbers.Number) else v
        for k, v in values.items()
    }
    return df_res, formatted
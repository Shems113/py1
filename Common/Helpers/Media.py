import hashlib
import mimetypes
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from mutagen.aac import AAC
from mutagen.flac import FLAC
from mutagen.m4a import M4A
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

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

def get_ffmpeg_version() -> str:
    """Retourne la première ligne de version de FFmpeg."""
    try:
        res = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            check=True,
        )
        lines = res.stdout.splitlines()
        return lines[0].strip() if lines else "Version inconnue"
    except (subprocess.SubprocessError, FileNotFoundError):
        return "ffmpeg introuvable dans le PATH"

def ensure_ffmpeg_available(verbose: bool = True) -> str | None:
    """Vérifie la présence de ffmpeg."""
    ffmpeg_path = shutil.which("ffmpeg")
    if verbose:
        print("Lien de ffmpeg : ", ffmpeg_path)
        if ffmpeg_path:
            print("Version de ffmpeg : ", get_ffmpeg_version())
        else:
            print("Version de ffmpeg : ffmpeg introuvable dans le PATH")
    return ffmpeg_path

def is_media_type(
    file_path: str | Path,
    media_type: str) -> bool:
    """Vérifie si le type MIME commence par le préfixe spécifié (ex: 'audio', 'video')."""
    mime = mimetypes.guess_type(str(file_path))[0] or ""
    return mime.startswith(media_type)

def is_audio(file_path: str | Path) -> bool:
    return is_media_type(file_path, "audio")

def is_video(file_path: str | Path) -> bool:
    return is_media_type(file_path, "video")

def is_text_file(file_path: str | Path) -> bool:
    return is_media_type(file_path, "text")

def is_media(file_path: str | Path) -> bool:
    return is_audio(file_path) or is_video(file_path) or is_text_file(file_path)

def move_media_file(
    source_path: str | Path,
    directory_name: str | Path,
    file_name_func: Callable[[int, str], str],
    is_same_media_func: Callable[[str | Path], bool],
    default_index: int = 0,
    is_move: bool = True,
) -> tuple[str, bool]:
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

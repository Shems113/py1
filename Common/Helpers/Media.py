import functools
import mimetypes
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from mutagen.aac import AAC
from mutagen.aiff import AIFF
from mutagen.asf import ASF
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

from Common.Helpers.File import (
    compute_file_hash,  # noqa: F401  (ré-export : ancien emplacement)
)

# Types MIME absents ou faux selon les machines (sous Windows, mimetypes lit le
# registre : .mkv ou .flac peuvent manquer, .ts est souvent pris pour du TypeScript).
# Enregistrés une fois au chargement du module, après l'initialisation de mimetypes.
_EXTRA_MIME_TYPES = {
    # Vidéo
    ".mkv": "video/x-matroska", ".webm": "video/webm", ".mp4": "video/mp4",
    ".m4v": "video/mp4", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
    ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv", ".ts": "video/mp2t",
    ".m2ts": "video/mp2t", ".mts": "video/mp2t", ".vob": "video/mpeg",
    ".mpg": "video/mpeg", ".mpeg": "video/mpeg", ".ogv": "video/ogg",
    ".3gp": "video/3gpp",
    # Audio
    ".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".opus": "audio/opus", ".wav": "audio/wav", ".aif": "audio/aiff",
    ".aiff": "audio/aiff", ".wma": "audio/x-ms-wma", ".ape": "audio/x-ape",
    ".wv": "audio/x-wavpack", ".alac": "audio/mp4",
    # Sous-titres (traités comme du texte)
    ".srt": "text/plain", ".ass": "text/plain", ".ssa": "text/plain",
    ".vtt": "text/vtt", ".sub": "text/plain",
}

def register_media_mime_types()-> None:
    """(Ré)enregistre les types MIME média. Appelé au chargement du module ;
    à rappeler si un autre code exécute mimetypes.init() (qui les efface)."""
    if not mimetypes.inited:
        mimetypes.init()
    for ext, mime in _EXTRA_MIME_TYPES.items():
        mimetypes.add_type(mime, ext)

register_media_mime_types()

# Mappings des types MIME (conservés pour compatibilité ; préférer mutagen.File,
# qui détecte le format d'après le contenu du fichier)
MIME_TYPE_TO_AUDIO = {
    "audio/aac": AAC,
    "audio/flac": FLAC,
    "audio/mpeg": MP3,
    "audio/ogg": OggVorbis,
    "audio/opus": OggOpus,    # l'Opus n'est pas du Vorbis
    "audio/wav": WAVE,
    "audio/x-wav": WAVE,
    "audio/aiff": AIFF,
    "audio/x-aiff": AIFF,
    "audio/mp4": MP4,         # .m4a ; mutagen.m4a.M4A est obsolète et lève une erreur
    "audio/x-m4a": MP4,
    "audio/x-ms-wma": ASF,
}
MIME_TYPE_TO_VIDEO = {"video/mp4": MP4}

@functools.cache
def get_ffmpeg_version()-> str:
    """Retourne la première ligne de version de FFmpeg (calculée une seule fois)."""
    try:
        res = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        lines = res.stdout.splitlines()
        return lines[0].strip() if lines else "Version inconnue"
    except (subprocess.SubprocessError, FileNotFoundError):
        return "ffmpeg introuvable dans le PATH"

def ensure_ffmpeg_available(verbose: bool = True)-> str | None:
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
    media_type: str)-> bool:
    """Vérifie si le type MIME commence par le préfixe spécifié (ex: 'audio', 'video')."""
    mime = mimetypes.guess_type(str(file_path))[0] or ""
    return mime.startswith(media_type)

def is_audio(file_path: str | Path)-> bool:
    return is_media_type(file_path, "audio")

def is_video(file_path: str | Path)-> bool:
    return is_media_type(file_path, "video")

def is_text_file(file_path: str | Path)-> bool:
    return is_media_type(file_path, "text")

def is_media(file_path: str | Path)-> bool:
    mime = mimetypes.guess_type(str(file_path))[0] or ""
    return mime.startswith(("audio", "video", "text"))  # un seul appel au lieu de trois

def move_media_file(
    source_path: str | Path,
    directory_name: str | Path,
    file_name_func: Callable[[int, str], str],
    is_same_media_func: Callable[[str | Path], bool],
    default_index: int = 0,
    is_move: bool = True,
)-> tuple[str, bool]:
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
        shutil.copy2(src, target_file_path)  # copy2 conserve aussi les dates du fichier

    return str(target_file_path), True

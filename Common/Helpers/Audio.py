import mimetypes
from pathlib import Path
from typing import Any

import ffmpeg  # Importation propre et officielle du package ffmpeg-python
from mutagen.aac import AAC
from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC
from mutagen.id3 import APIC, ID3
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


def extract_mono_16khz_pcm_stream(
    file_path: str | Path,
    logger: Any | None = None
) -> bytes:
    """Extrait l'audio d'un fichier média sous forme de flux PCM 16-bit 16 kHz Mono en mémoire."""
    path_obj = Path(file_path).resolve()

    if not path_obj.exists():
        if logger:
            logger.logError(f"Fichier introuvable : {path_obj}")
        return b""

    try:
        out, _ = (
            ffmpeg
            .input(str(path_obj))
            .output(
                'pipe:',
                format='s16le',
                acodec='pcm_s16le',
                ac=1,
                ar='16000',
                vn=None,  # Ignore le flux vidéo
                sn=None   # Ignore les sous-titres
            )
            .run(capture_stdout=True, capture_stderr=True)
        )
        return out

    except ffmpeg.Error as e:
        if logger:
            error_msg = e.stderr.decode('utf-8', errors='ignore') if e.stderr else str(e)
            logger.logError(f"Échec de l'extraction audio FFmpeg pour {path_obj.name} : {error_msg}")
        return b""

def get_audio_id3_tags(file_path: str | Path) -> Any:
    """Extrait les étiquettes ID3/EasyID3 d'un fichier audio."""
    path_str = str(file_path)
    mime = mimetypes.guess_type(path_str)[0]
    audio_cls = MIME_TYPE_TO_AUDIO.get(mime)
    return audio_cls(path_str, ID3=EasyID3) if audio_cls else None

def get_audio_tags(file_path: str | Path) -> Any:
    """Extrait les étiquettes de métadonnées vidéo/audio de base."""
    path_str = str(file_path)
    mime = mimetypes.guess_type(path_str)[0]
    video_cls = MIME_TYPE_TO_VIDEO.get(mime)
    return video_cls(path_str) if video_cls else None

def add_audio_id3_images(
    file_path: str | Path,
    image_data_dico: dict[str | Path, dict[int, str]]
) -> None:
    """Ajoute des pochettes (images APIC) dans un fichier MP3/ID3."""
    path_str = str(file_path)
    audio = ID3(path_str)

    # Nettoyage des couvertures existantes
    for tag in [t for t in audio if t.startswith("APIC")]:
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


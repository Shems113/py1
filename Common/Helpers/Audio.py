"""Helpers de lecture / écriture de métadonnées audio (mutagen) et
d'extraction PCM (ffmpeg-python)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import ffmpeg  # package ffmpeg-python
import mutagen
from mutagen.id3 import APIC, ID3
from mutagen.mp3 import MP3

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _image_mime(data: bytes) -> str:
    """Type MIME réel d'une image (les pochettes peuvent être en PNG)."""
    if data.startswith(_PNG_MAGIC):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def extract_mono_16khz_pcm_stream(
    file_path: str | Path,
    logger: Any | None = None,
) -> bytes:
    """Extrait l'audio d'un fichier média en PCM 16-bit, 16 kHz, mono, en mémoire.
    Renvoie b"" en cas d'échec. Mémoire : ~1,9 Mo par minute d'audio."""
    path_obj = Path(file_path).resolve()

    if not path_obj.is_file():
        if logger:
            logger.log_error(f"Fichier introuvable : {path_obj}")
        return b""

    try:
        out, _ = (
            ffmpeg
            .input(str(path_obj))
            .output("pipe:", format="s16le", acodec="pcm_s16le", ac=1, ar=16000,
                    vn=None, sn=None)  # ignore vidéo et sous-titres
            # -nostdin : sans ça, ffmpeg lit la console et peut bloquer ou
            # « avaler » des frappes quand il tourne dans un pool de threads
            .global_args("-nostdin", "-hide_banner", "-loglevel", "error")
            .run(capture_stdout=True, capture_stderr=True)
        )
        return out

    except ffmpeg.Error as e:
        if logger:
            error_msg = e.stderr.decode("utf-8", errors="ignore") if e.stderr else str(e)
            logger.log_error(f"Échec de l'extraction audio FFmpeg pour {path_obj.name} : {error_msg}")
        return b""


def get_audio_id3_tags(file_path: str | Path) -> Any:
    """Tags « simplifiés » (clés communes : title, artist, album, albumartist,
    tracknumber, discnumber, date, genre…) pour MP3, FLAC, OGG, Opus, M4A…
    Le format est détecté d'après le contenu, pas l'extension.
    Renvoie None si le format n'est pas reconnu.

    Attention : mutagen n'a pas de mode simplifié pour WAV et AIFF, leurs tags
    restent des frames ID3 brutes (TIT2, TPE1…). Pour des clés identiques quel
    que soit le format, utiliser read_common_tags()."""
    return mutagen.File(str(file_path), easy=True)


# Frames ID3 -> clés simplifiées, pour WAV / AIFF
_ID3_TO_EASY = {"TIT2": "title", "TPE1": "artist", "TPE2": "albumartist", "TALB": "album",
                "TRCK": "tracknumber", "TPOS": "discnumber", "TDRC": "date", "TCON": "genre"}


def read_common_tags(file_path: str | Path) -> dict[str, str]:
    """Tags usuels sous forme {clé: valeur}, mêmes clés pour tous les formats
    (title, artist, albumartist, album, tracknumber, discnumber, date, genre).
    Renvoie {} si le fichier est illisible ou sans tags."""
    try:
        audio = mutagen.File(str(file_path), easy=True)
    except mutagen.MutagenError:
        return {}
    if audio is None or not audio.tags:
        return {}
    if isinstance(audio.tags, ID3):  # WAV / AIFF : frames brutes
        items = ((easy, audio.tags.get(frame)) for frame, easy in _ID3_TO_EASY.items())
        return {k: str(f.text[0]) for k, f in items if f is not None and f.text}
    return {k: str(v[0]) for k, v in audio.tags.items()
            if k in _ID3_TO_EASY.values() and v and str(v[0]).strip()}


def get_audio_tags(file_path: str | Path) -> Any:
    """Tags bruts du format natif (frames ID3, atomes MP4, Vorbis comments…),
    pour tout fichier audio ou vidéo MP4 reconnu. Renvoie None sinon."""
    return mutagen.File(str(file_path))


def add_audio_id3_images(
    file_path: str | Path,
    image_data_dico: dict[str | Path, dict[int, str]],
) -> None:
    """Remplace les pochettes (APIC) d'un fichier à tags ID3 (MP3, WAV, AIFF).

    image_data_dico : {chemin_image: {type_APIC: description}}
                      ex. {"cover.jpg": {3: "Cover"}}  (3 = couverture avant)
    """
    path_str = str(file_path)
    audio = mutagen.File(path_str)
    if audio is None:
        raise ValueError(f"Format audio non reconnu : {path_str}")
    if audio.tags is None:
        audio.add_tags()  # fichier sans aucun tag : on crée l'en-tête ID3
    if not isinstance(audio.tags, ID3):
        raise ValueError(f"Pas de tags ID3 pour ce format ({type(audio).__name__}) : {path_str}")

    audio.tags.delall("APIC")  # suppression des pochettes existantes
    for img_path, img_type_dico in image_data_dico.items():
        img_bytes = Path(img_path).read_bytes()
        mime = _image_mime(img_bytes)
        for img_type, img_desc in img_type_dico.items():
            audio.tags.add(APIC(encoding=3, mime=mime, type=img_type, desc=img_desc,
                                data=img_bytes))

    # ID3v2.3 pour le MP3 : version la mieux lue par Windows, autoradios, baladeurs
    if isinstance(audio, MP3):
        audio.save(v2_version=3)
    else:
        audio.save()
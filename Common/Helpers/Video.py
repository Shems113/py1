import json
import mimetypes
import subprocess
from pathlib import Path
from typing import Any, BinaryIO

import av
import ffmpeg
import numpy as np  # Importation propre et officielle du package ffmpeg-python
from av.codec.hwaccel import HWAccel

from Common.Helpers.System import IS_MY_GPU_MACHINE


def _open_container(source: str | BinaryIO) -> Any:
    """Ouvre le conteneur, en décodage GPU si USE_GPU, sinon (ou en cas d'échec) en décodage CPU."""
    if IS_MY_GPU_MACHINE:
        try:
            print("USE_GPU")
            return av.open(source, hwaccel=HWAccel(device_type="d3d11va", allow_software_fallback=True))
        except (OSError, av.FFmpegError):
            if not isinstance(source, str):
                source.seek(0)  # flux déjà entamé : on le rembobine avant de réessayer en CPU
    return av.open(source)

def get_video_extensions() -> set[str]:
    """Retourne l'ensemble de toutes les extensions de fichiers vidéo référencées par mimetypes."""
    mimetypes.init()
    return {
        ext.lower()
        for ext, mime in mimetypes.types_map.items()
        if mime.startswith("video/")
    }

def get_video_streams_and_chapters(
    input_source: str | Path | BinaryIO
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Récupère simultanément les flux vidéo/audio et les chapitres via ffprobe.

    Accepte un chemin de fichier (str/Path) ou un flux streaming (ex: sys.stdin.buffer).
    """
    try:
        if isinstance(input_source, (str, Path)):
            probe_data = ffmpeg.probe(str(input_source), show_chapters=None)
        else:
            # Si input_source est un flux streaming (pipe SSH / bytes), on le passe sur stdin via subprocess
            input_bytes = input_source.read() if hasattr(input_source, 'read') else input_source
            cmd = [
                'ffprobe', '-show_format', '-show_streams', '-show_chapters',
                '-print_format', 'json', '-i', 'pipe:0'
            ]
            process = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            out, _ = process.communicate(input=input_bytes)
            probe_data = json.loads(out.decode('utf-8'))

        streams = probe_data.get("streams", [])
        chapters = probe_data.get("chapters", [])
        return streams, chapters

    except Exception:  # noqa: BLE001
        return [], []

def get_video_corrupted_frames_info(
    input_source: str | Path | BinaryIO
) -> dict[str, Any]:
    """Décode le flux avec PyAV, compte les frames invalides et calcule le ratio de corruption.
    Accepte un chemin de fichier (str/Path) ou un flux streaming reçu par SSH (ex: sys.stdin.buffer).
    Sur Windows (TX16PRO), le décodage passe par le GPU (D3D11VA) pour les codecs qu'il supporte.
    """
    total_frames = 0
    corrupted_frames = 0
    first_error = None
 
    try:
        source = str(input_source) if isinstance(input_source, (str, Path)) else input_source
        with _open_container(source) as container:
            stream = container.streams.video[0]
            hw_decoding = stream.codec_context.is_hwaccel
 
            # Parcours paquet par paquet pour continuer le scan même après une erreur
            for packet in container.demux(stream):
                try:
                    for _ in packet.decode():
                        total_frames += 1
                except av.FFmpegError as e:
                    corrupted_frames += 1
                    if not first_error:
                        first_error = str(e)
 
    except Exception as e:  # noqa: BLE001
        return {
            "total_frames": 0,
            "error": f"Erreur ouverture/I-O: {e!s}",
            "corrupted_count": None,
            "corruption_ratio": 0.0,
            "hw_decoding": None,
        }
 
    # Calcul du ratio
    processed_total = total_frames + corrupted_frames
    ratio = (corrupted_frames / processed_total) if processed_total > 0 else 0.0
 
    return {
        "total_frames": processed_total,
        "error": first_error,
        "corrupted_count": corrupted_frames,
        "corruption_ratio": round(ratio, 4),  # ex: 0.0345
        "hw_decoding": hw_decoding,
    }

def _stream_duration_sec(stream: Any, container: Any) -> float | None:
    """Durée en secondes : durée du flux (en unités time_base), sinon durée du conteneur."""
    if stream.duration is not None and stream.time_base is not None:
        return float(stream.duration * stream.time_base)
    if container.duration is not None:
        return container.duration / av.time_base  # container.duration est en microsecondes
    return None


def _to_float_samples(frame: Any) -> np.ndarray:
    """Convertit une frame audio en échantillons float normalisés dans [-1, 1]."""
    samples = frame.to_ndarray()
    if np.issubdtype(samples.dtype, np.integer):
        return samples.astype(np.float64) / float(np.iinfo(samples.dtype).max)
    return samples.astype(np.float64, copy=False)


def _to_db(value: float) -> float:
    return round(20.0 * float(np.log10(value)), 2) if value > 0 else -120.0

def get_video_quality_indicators(
    input_source: str | Path | BinaryIO
) -> dict[str, Any]:
    """Analyse complète d'une vidéo en une seule passe de décodage.

    Accepte un chemin de fichier (str/Path) ou un flux binaire (ex: sys.stdin.buffer).
    Fonctionne avec tout codec supporté par FFmpeg (source ou AV1 post-encodage).
    """
    # Compteurs vidéo
    video_packets = 0
    video_bytes = 0
    corrupted_video_packets = 0
    decoded_frames = 0
    keyframe_count = 0
    frame_pts: list[int] = []
    first_error: str | None = None

    # Compteurs audio
    audio_frames = 0
    corrupted_audio_packets = 0
    audio_end_sec: float | None = None
    audio_sum_squares = 0.0
    audio_sample_count = 0
    audio_peak = 0.0

    try:
        source = str(input_source) if isinstance(input_source, (str, Path)) else input_source
        with av.open(source) as container:
            if not container.streams.video:
                return {"status": "error", "error": "Aucun flux vidéo trouvé", "indicators": {}}

            video_stream = container.streams.video[0]
            video_stream.thread_type = "AUTO"  # décodage multi-thread
            audio_stream = container.streams.audio[0] if container.streams.audio else None
            streams = [s for s in (video_stream, audio_stream) if s is not None]

            # Une seule passe : un flux pipe ne peut être lu qu'une fois
            for packet in container.demux(*streams):
                is_video = packet.stream.type == "video"

                if is_video and packet.size > 0:
                    video_packets += 1
                    video_bytes += packet.size
                    if packet.is_keyframe:
                        keyframe_count += 1

                try:
                    frames = packet.decode()
                except av.FFmpegError as e:
                    if is_video:
                        corrupted_video_packets += 1
                        if first_error is None:
                            first_error = str(e)
                    else:
                        corrupted_audio_packets += 1
                    continue

                for frame in frames:
                    if is_video:
                        decoded_frames += 1
                        if frame.pts is not None:
                            frame_pts.append(frame.pts)
                    else:
                        audio_frames += 1
                        if frame.pts is not None and frame.sample_rate:
                            end = float(frame.pts * frame.time_base) + frame.samples / frame.sample_rate
                            audio_end_sec = end if audio_end_sec is None else max(audio_end_sec, end)
                        samples = _to_float_samples(frame)
                        if samples.size:
                            audio_sum_squares += float(np.sum(samples * samples))
                            audio_sample_count += samples.size
                            audio_peak = max(audio_peak, float(np.max(np.abs(samples))))

            # === Métadonnées ===
            rate = video_stream.average_rate or video_stream.guessed_rate
            fps = float(rate) if rate else None
            time_base = float(video_stream.time_base) if video_stream.time_base else None
            declared_duration = _stream_duration_sec(video_stream, container)
            declared_bitrate = video_stream.bit_rate or container.bit_rate or 0
            resolution = f"{video_stream.width}x{video_stream.height}"
            codec = video_stream.codec_context.codec
            codec_name = getattr(codec, "canonical_name", None) or codec.name or "unknown"
            has_audio = audio_stream is not None

    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"Erreur ouverture/I-O: {e}", "indicators": {}}

    # === 1. Corruption (ratio sur les paquets vidéo) ===
    corruption_ratio = corrupted_video_packets / video_packets if video_packets else 0.0

    # === 2. Complétude : frames décodées vs frames attendues ===
    expected_frames = round(declared_duration * fps) if declared_duration and fps else None
    # Tolérance d'1 frame : arrondi de la durée déclarée par le conteneur (ex: MKV à la ms)
    frame_loss_ratio = (
        max(0, expected_frames - decoded_frames - 1) / expected_frames if expected_frames else None
    )

    # === 3. Timeline vidéo mesurée + jitter (PTS triés : ordre de présentation) ===
    jitter_ms = None
    video_end_sec = None
    if frame_pts and time_base:
        pts_sec = np.sort(np.array(frame_pts, dtype=np.float64)) * time_base
        frame_dur = 1.0 / fps if fps else 0.0
        video_end_sec = float(pts_sec[-1]) + frame_dur
        if len(pts_sec) > 2:
            jitter_ms = float(np.std(np.diff(pts_sec) * 1000.0))
    measured_duration = video_end_sec if video_end_sec is not None else declared_duration

    # === 4. Bitrate mesuré (octets des paquets vidéo réellement lus) ===
    measured_bitrate = (video_bytes * 8 / measured_duration) if measured_duration else 0.0
    bitrate_deviation = (
        abs(measured_bitrate - declared_bitrate) / declared_bitrate * 100 if declared_bitrate else None
    )

    # === 5. GOP ===
    keyframe_interval = video_packets / keyframe_count if keyframe_count else 0.0
    gop_health = (
        "ok" if 20 < keyframe_interval < 600
        else "warning" if keyframe_interval > 0
        else "error"
    )

    # === 6. Audio : désynchro mesurée sur les fins de pistes + niveaux ===
    av_drift_ms = (
        abs(video_end_sec - audio_end_sec) * 1000.0
        if video_end_sec is not None and audio_end_sec is not None else None
    )
    audio_rms = (audio_sum_squares / audio_sample_count) ** 0.5 if audio_sample_count else 0.0

    # === Qualité globale ===
    loss = frame_loss_ratio if frame_loss_ratio is not None else 0.0
    if corruption_ratio < 0.001 and loss < 0.01 and gop_health == "ok":
        overall = "excellent"
    elif corruption_ratio < 0.01 and loss < 0.05:
        overall = "good"
    elif corruption_ratio < 0.05 and loss < 0.1:
        overall = "acceptable"
    else:
        overall = "poor"

    def _r(
        value: float | None,
        digits: int
        ) -> float | None:
        return round(value, digits) if value is not None else None

    return {
        "status": "success",
        "error": first_error,
        "indicators": {
            "corruption": {
                "corrupted_count": corrupted_video_packets,
                "corruption_ratio": round(corruption_ratio, 6),
                "total_packets": video_packets,
            },
            "frame_integrity": {
                "expected_frames": expected_frames,
                "decoded_frames": decoded_frames,
                "frame_loss_ratio": _r(frame_loss_ratio, 6),
                "frame_loss_percent": _r(frame_loss_ratio * 100 if frame_loss_ratio is not None else None, 2),
            },
            "temporal_stability": {
                "jitter_ms": _r(jitter_ms, 4),
                "jitter_warning": None if jitter_ms is None else ("high" if jitter_ms > 5 else "ok"),
            },
            "bitrate": {
                "declared_mbps": round(declared_bitrate / 1e6, 2),
                "measured_mbps": round(measured_bitrate / 1e6, 2),
                "deviation_percent": _r(bitrate_deviation, 2),
            },
            "gop_structure": {
                "keyframe_count": keyframe_count,
                "keyframe_interval_frames": round(keyframe_interval, 1),
                "gop_health": gop_health,
            },
            "audio": {
                "has_audio": has_audio,
                "decoded_audio_frames": audio_frames,
                "corrupted_audio_packets": corrupted_audio_packets,
                "peak_level_db": _to_db(audio_peak) if has_audio else None,
                "rms_level_db": _to_db(audio_rms) if has_audio else None,
                "av_sync_drift_ms": _r(av_drift_ms, 2),
                "av_sync_ok": None if av_drift_ms is None else av_drift_ms < 50,
            },
            "metadata": {
                "codec": codec_name,
                "resolution": resolution,
                "fps": _r(fps, 3),
                "declared_duration_seconds": _r(declared_duration, 2),
                "measured_duration_seconds": _r(measured_duration, 2),
            },
            "overall_quality": overall,
        },
    }

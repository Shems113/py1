from pathlib import Path
import struct
import sys
from typing import Any, Dict, List, Union

import numpy as np
import pandas as pd

from Common.CUtils import (
    data_frame_from_csv,
    data_frame_to_csv,
    ensure_ffmpeg_available,
    format_timestamp,
    get_audio_id3_tags,
    get_audio_tags,
    get_file_common_fields,
    get_video_streams_and_chapters,
    is_audio,
    is_text_file,
    is_video,
    json_to_stdout,
)
from Common.CWhisperModel import CWhisperModel
from Common.EEnums import EActionType, EMediaType
from Media.CMedia import CMedia


class CVideo(CMedia):
    def __init__(
        self,
        json_path: Union[str, Path],
        action_type: EActionType,
        is_streaming: bool,
        media_type: EMediaType = EMediaType.Video,
    ) -> None:
        super().__init__(json_path, media_type, action_type, is_streaming)
        self.whisper_model = CWhisperModel(
            logger=self.logger, model_size_or_path="large-v3"
        )

    def read(self) -> None:
        """Lit les flux et métadonnées vidéo et les exporte sous forme de fichiers CSV."""
        ensure_ffmpeg_available()
        data = self.process_files_parallel(self._get_video_attributes_fields)
        dfs = self.build_dataframes_by_codec(data)
        self.export_dataframes_to_csv_by_codec(dfs)

    def _get_video_attributes_fields(
        self, file_path: Union[str, Path]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Extrait les flux, chapitres et métadonnées d'un fichier vidéo/texte."""
        path_obj = Path(file_path)
        self.logger.log(f"Get video attributes fields of {path_obj}")
        result: Dict[str, List[Dict[str, Any]]] = {}

        if not (is_video(path_obj) or is_text_file(path_obj)):
            return result

        try:
            streams, chapters = get_video_streams_and_chapters(path_obj)
            common_fields = get_file_common_fields(path_obj)

            # Regroupement des flux par type de codec (video, audio, subtitle, etc.)
            for stream in streams:
                codec_type = stream.get("codec_type")
                if codec_type:
                    result.setdefault(codec_type, []).append(common_fields | stream)

            # Traitement des chapitres
            if chapters:
                result["videoChapters"] = [
                    common_fields | chap for chap in chapters
                ]

            # Traitement des métadonnées additionnelles pour les vidéos
            if is_video(path_obj):
                audio_tags = dict(get_audio_tags(path_obj))
                result["videoOther"] = [common_fields | audio_tags]

        except Exception as e:
            self.logger.logError(
                f"Error with {path_obj} : failed to get fields ({e})"
            )

        return result

    def write(self) -> None:
        """Édite les métadonnées audio/vidéo à partir des données d'un CSV source."""
        if not self.source_path:
            return

        df = data_frame_from_csv(self.source_path, self.data_separator)

        if df.empty or "path" not in df.columns:
            return

        media_fields = [col for col in df.columns if col != "path"]
        rows_to_drop = []

        for row in df.itertuples():
            file_path = row.path
            fields_data = {
                col: getattr(row, col)
                for col in media_fields
                if pd.notna(getattr(row, col))
            }

            def edit_task() -> bool:
                return self._edit_audio_fields(file_path, fields_data)

            log_msg = f"Edit {file_path} with fields {media_fields}"
            if self.logger.logElapsedTime(edit_task, log_msg):
                rows_to_drop.append(row.Index)

        if rows_to_drop:
            df = df.drop(index=rows_to_drop)

        data_frame_to_csv(
            df,
            self.source_path,
            self.data_separator,
            self.fieldsByCodec.get("video"),
        )

    def _edit_audio_fields(
        self, file_path: Union[str, Path], fields: Dict[str, Any]
    ) -> bool:
        """Met à jour les étiquettes (tags) d'un fichier média audio ou vidéo."""
        path_obj = Path(file_path)
        if not (is_audio(path_obj) or is_video(path_obj)):
            return False

        try:
            fetch_tags_fn = (
                get_audio_id3_tags if is_audio(path_obj) else get_audio_tags
            )
            audio = fetch_tags_fn(path_obj)

            updates = {}
            for key, value in fields.items():
                current_val = audio.get(key, [None])[0]
                str_val = str(value)
                if str_val != current_val:
                    updates[key] = str_val.split("@")

            if updates:
                audio.update(updates)
                audio.save(path_obj)
                self.logger.log(f"Update {path_obj} tags with {updates}")

            return True

        except Exception as e:
            self.logger.logError(
                f"Error with {path_obj} : failed to edit tags ({e})"
            )
            return False

    def transcribe(self) -> None:
        """Exécute la transcription audio et la synchronisation."""
        if self.is_streaming:
            data = self.transcribe_video_audio_stream()
            json_to_stdout(data)
        else:
            data = self.process_files_parallel(self.transcribe_video_audio_file)
            dfs = self.build_dataframes_by_codec(
                data, log_message="Audio transcription map generated for "
            )
            self.export_dataframes_to_csv_by_codec(dfs)

    def transcribe_video_audio_file(
        self,
        file_path: Union[str, Path],
        add_srt: bool = False,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Transcrit un fichier vidéo/audio et génère soit des commandes FFmpeg, soit des blocs SRT."""
        path_obj = Path(file_path).resolve()
        segment_data: List[Dict[str, Any]] = []
        result: Dict[str, List[Dict[str, Any]]] = {}

        if not (is_video(path_obj) or is_audio(path_obj)):
            return result

        try:
            segment_data = self.logger.logElapsedTime(
                lambda: self.whisper_model.get_file_transcription_data(
                    path_obj,
                    # Décodage & Précision
                    beam_size=5,
                    patience=1.0,
                    best_of=5,
                    temperature=(0.0, 0.2, 0.4),
                    # Stabilité (Empêche les répétitions en boucle lors des silences/musiques)
                    condition_on_previous_text=False,
                    # Filtrage VAD (Découpe propre des dialogues & ajouts de marges aux mots)
                    voice_activity_detection_filter=True,
                    voice_activity_detection_parameters={
                        "threshold": 0.5,
                        "min_speech_duration_ms": 250,
                        "max_speech_duration_s": 12.0,  # Utile pour éviter les sous-titres à rallonge
                        "min_silence_duration_ms": 500,
                        "speech_pad_ms": 400,  # Marge de 400ms pour ne pas couper l'attaque des phrases
                    },
                    # Horodatage précis au niveau du mot (si supporté par ta méthode)
                    word_timestamps=True,
                ),
            f"Processing {path_obj.name} to get transcription data...",
        )

            self.logger.log(
                f"Transcription result of {path_obj.name} -> {len(segment_data)} blocs."
            )

            audio_transcription = [
                {
                    "path": str(path_obj),
                    "index": i,
                    "start": segment["start"],
                    "end": segment["end"],
                    "duration": segment["end"] - segment["start"],
                    "text": segment["text"],
                    "data": self._get_data(0 if add_srt else 1, i, segment),
                }
                for i, segment in enumerate(segment_data)
            ]

            result = {"audioTranscription": audio_transcription}
            self.logger.log(f"Transcription successful: {path_obj.name}")

        except Exception as ex:
            self.logger.logError(f"Error transcribing {path_obj.name}: {ex}")

        return result

    def transcribe_video_audio_stream(
        self, 
        add_srt: bool = False, 
        chunk_size: int = 65536
    ) -> List[Dict[str, Any]]:
        """Lit un flux audio PCM 16-bit 16kHz Mono depuis stdin jusqu'à EOF et retourne sa transcription."""
        stream = sys.stdin.buffer
        chunks = []
        total_bytes = 0

        self.logger.log("Reading streaming audio from stdin until EOF...")

        # 1. Lecture continue par chunks jusqu'à la fermeture du flux (EOF)
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            chunks.append(chunk)
            total_bytes += len(chunk)

        raw_data = b"".join(chunks)

        # 2. Validation du payload PCM 16-bit brut
        if not self._validate_raw_pcm_stream(raw_data):
            return []

        # 3. Conversion PCM int16 -> float32 normalisé [-1.0, 1.0]
        audio = (
            np.frombuffer(raw_data, dtype=np.int16).astype(np.float32) / 32768.0
        )

        # 4. Validation finale du tableau NumPy
        if not self._validate_audio_array(audio):
            return []

        # Inférence Whisper
        segment_data = self.logger.logElapsedTime(
            lambda: self.whisper_model.get_file_transcription_data(audio),
            "Processing audio stream for transcription...",
        )

        if not segment_data:
            return []

        audio_transcription = [
            {
                "stream": "stdin",
                "index": i,
                "start": segment["start"],
                "end": segment["end"],
                "duration": segment["end"] - segment["start"],
                "data": self._get_data(0 if add_srt else -1, i, segment),
            }
            for i, segment in enumerate(segment_data)
        ]

        self.logger.log("Transcription successful for stdin stream")
        return audio_transcription

    def _validate_raw_pcm_stream(self, raw_data: bytes) -> bool:
        """Contrôle d'intégrité du buffer binaire PCM avant conversion."""
        if len(raw_data) == 0:
            self.logger.logError("Stream payload is empty (0 bytes received).")
            return False

        if len(raw_data) % 2 != 0:
            self.logger.logError(
                f"Invalid PCM 16-bit alignment: payload size ({len(raw_data)} bytes) is not a multiple of 2."
            )
            return False

        return True

    def _validate_audio_array(self, audio: np.ndarray) -> bool:
        """Validation stricte des dimensions et du format du tableau NumPy converti."""
        if audio.dtype != np.float32:
            self.logger.logError(
                f"Invalid audio dtype: expected float32, got {audio.dtype}"
            )
            return False

        if audio.ndim != 1:
            self.logger.logError(
                f"Invalid audio layout: expected 1D (mono), got shape {audio.shape}"
            )
            return False

        if audio.size == 0:
            self.logger.logError("Converted audio array is empty.")
            return False

        return True

    def _get_data(self, data_type: int, i: int, seg: Dict[str, Any]) -> str:
        self.logger.log(f"Get data of the segment {i} : {seg}")

        if data_type == 0:
            start = seg["start"]
            end = seg["end"]
            text = seg["text"]
            return f"{i + 1}\n{format_timestamp(start)} --> {format_timestamp(end)}\n{text.strip()}\n\n"

        if data_type == 1:
            ffmpeg_cmd = [
                "ffmpeg",
                "-y",
                "-ss",
                "@audio_start",
                "-to",
                "@audio_end",
                "-i",
                "@audio_path",
                "-af",
                "@atempo_or_anull",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(Path(self.target_path) / f"chunk_{i:04d}.m4a"),
            ]
            return " ".join(ffmpeg_cmd)

        return seg["text"]
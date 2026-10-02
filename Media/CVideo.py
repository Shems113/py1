import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from Common.CWhisperModel import CWhisperModel
from Common.EEnums import EActionType, EMediaType
from Common.Helpers.Audio import (
    extract_mono_16khz_pcm_stream,
    get_audio_tags,
)
from Common.Helpers.Dataframe import data_frame_to_csv
from Common.Helpers.File import get_file_common_fields
from Common.Helpers.Media import (
    ensure_ffmpeg_available,
    is_audio,
    is_text_file,
    is_video,
)
from Common.Helpers.Other import format_timestamp
from Common.Helpers.Video import (
    get_video_corrupted_frames_info,
    get_video_streams_and_chapters,
)
from Media.CMedia import CMedia


class CVideo(CMedia):
    def __init__(
        self,
        json_path: str | Path,
        action_type: EActionType,
        media_type: EMediaType = EMediaType.Video,
        whisper_workers: int = 1,
        )-> None:
        super().__init__(json_path, media_type, action_type)
        # Modèle chargé seulement à la première transcription (CLazy)
        self.whisper_model = CWhisperModel(
            logger=self.logger, model_size_or_path="large-v3", num_workers=whisper_workers
        )
        self.chunk_stream_size: int = 65536

    def read(self)-> None:
        """Lit les flux et métadonnées vidéo et les exporte sous forme de fichiers CSV."""
        if self.is_streaming:
            frame_errors = {}
            if self.action_type == EActionType.ReadAndCheck:
                frame_errors = self.logger.log_elapsed_time(
                    lambda: get_video_corrupted_frames_info(sys.stdin.buffer),
                    "Video frames checking from input stream..."
                )

            df = pd.DataFrame([frame_errors] if frame_errors else [])
            data_frame_to_csv(
                df=df,
                target=sys.stdout,
                separator=self.data_separator
            )
            self.logger.log("Writing csv to output stream successful")
        else:
            ensure_ffmpeg_available()
            data = self.process_files_parallel(self._get_video_attributes_fields)
            dfs = self.build_dataframes_by_codec(data)
            self.export_dataframes_to_csv_by_codec(dfs)

    def _get_video_attributes_fields(
        self,
        file_path: str | Path
    )-> dict[str, list[dict[str, Any]]]:
        """Extrait les flux, chapitres et métadonnées d'un fichier vidéo/texte."""
        path_obj = Path(file_path)
        self.logger.log(f"Get video attributes fields of {path_obj}")
        result: dict[str, list[dict[str, Any]]] = {}

        if not (is_video(path_obj) or is_text_file(path_obj)):
            return result

        try:
            common_fields = get_file_common_fields(path_obj)
            streams, chapters = get_video_streams_and_chapters(path_obj)

            for stream in streams:
                codec_type = stream.get("codec_type")
                if codec_type:
                    result.setdefault(codec_type, []).append(common_fields | stream)

            if chapters:
                result["videoChapters"] = [
                    common_fields | chap for chap in chapters
                ]

            if is_video(path_obj):
                if self.action_type == EActionType.ReadAndCheck:
                    frame_errors = self.logger.log_elapsed_time(
                        lambda: get_video_corrupted_frames_info(path_obj),
                        f"Video frames checking of {path_obj}..."
                    )
                    if frame_errors:
                        result["frameErrors"] = [common_fields | frame_errors]

                # mutagen ne lit que certains conteneurs (MP4, M4V…) : rien pour un MKV
                audio_tags = get_audio_tags(path_obj)
                if audio_tags is not None:
                    result["videoOther"] = [common_fields | dict(audio_tags)]

        except Exception as e:  # noqa: BLE001
            self.logger.log_error(
                f"Error with {path_obj} : failed to get fields ({e})"
            )

        return result

    def write(self)-> None:
        """Édite les métadonnées audio/vidéo à partir des données d'un CSV source."""
        self._write_tags_from_csv("video")

    def transcribe(self)-> None:
        """Exécute la transcription audio et la synchronisation."""
        if self.is_streaming:
            data = self._transcribe_video_audio_stream()
            df = pd.DataFrame(data)
            data_frame_to_csv(
                df=df,
                target=sys.stdout,
                separator=self.data_separator,
                fields=self.fields_by_codec.get("audioTranscription"),
            )
        else:
            data_by_codec = self.process_files_parallel(self.transcribe_video_audio_file)
            dfs = self.build_dataframes_by_codec(
                data_by_codec, log_message="Audio transcription map generated for ", skip_aggregation=True)
            self.export_dataframes_to_csv_by_codec(dfs)

    def transcribe_video_audio_file(
        self,
        file_path: str | Path
    )-> dict[str, list[dict[str, Any]]]:
        """Convertit un fichier média en flux PCM (helper Audio) et le transcrit."""
        path_obj = Path(file_path).resolve()
        if not (is_video(path_obj) or is_audio(path_obj)):
            return {}

        try:
            self.logger.log(f"Extracting PCM stream from file: {path_obj.name}")

            raw_pcm = extract_mono_16khz_pcm_stream(path_obj)

            if not raw_pcm:
                self.logger.log_error(
                    f"FFmpeg stream extraction returned empty data for {path_obj.name}"
                )
                return {}

            audio_transcription = self._transcribe_video_audio_stream(
                stream_input=raw_pcm,
                source_name=str(path_obj),
            )

            self.logger.log(f"Transcription successful: {path_obj.name}")
            return {"audioTranscription": audio_transcription}

        except Exception as ex:  # noqa: BLE001
            self.logger.log_error(f"Error transcribing {path_obj.name}: {ex}")
            return {}

    def _transcribe_video_audio_stream(
        self,
        stream_input: bytes | None = None,
        source_name: str = "stdin",
    )-> list[dict[str, Any]]:
        """Lit un flux audio PCM 16-bit 16kHz Mono (depuis stdin ou un paramètre bytes) et exécute Whisper."""
        if stream_input is None:
            stream_input = self._chunk_input_stream()

        if not self._validate_raw_pcm_stream(stream_input):
            return []

        audio = np.frombuffer(stream_input, dtype=np.int16).astype(np.float32) / 32768.0

        if not self._validate_audio_array(audio):
            return []

        segment_data = self.logger.log_elapsed_time(
            lambda: self.whisper_model.get_file_transcription_data(
                audio,
                beam_size=5,
                patience=1.0,
                best_of=5,
                temperature=(0.0, 0.2, 0.4),
                condition_on_previous_text=False,
                voice_activity_detection_filter=True,
                voice_activity_detection_parameters={
                    "threshold": 0.5,
                    "min_speech_duration_ms": 250,
                    "max_speech_duration_s": 12.0,
                    "min_silence_duration_ms": 500,
                    "speech_pad_ms": 400,
                },
                word_timestamps=True,
            ),
            f"Processing audio stream ({source_name}) for transcription...",
        )

        if not segment_data:
            return []

        is_file_source = source_name != "stdin"

        audio_transcription = [
            {
                "path" if is_file_source else "stream": source_name,
                "index": i,
                "start": segment["start"],
                "end": segment["end"],
                "duration": segment["end"] - segment["start"],
                "text": segment["text"],
                "srt": self._segment_to_srt(i, segment),
                "ffmpeg_cmd": self._segment_to_ffmpeg_cmd(i),
            }
            for i, segment in enumerate(segment_data)
        ]

        self.logger.log(f"Transcription successful for {source_name}")
        return audio_transcription

    def _chunk_input_stream(self)-> bytes:
        """Lit le flux binaire depuis stdin jusqu'à EOF."""
        self.logger.log("Reading streaming from stdin until EOF...")
        stream = sys.stdin.buffer
        chunks = []
        while True:
            chunk = stream.read(self.chunk_stream_size)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    def _validate_raw_pcm_stream(self, raw_data: bytes)-> bool:
        """Contrôle d'intégrité du buffer binaire PCM avant conversion."""
        if len(raw_data) == 0:
            self.logger.log_error("Stream payload is empty (0 bytes received).")
            return False

        if len(raw_data) % 2 != 0:
            self.logger.log_error(
                f"Invalid PCM 16-bit alignment: payload size ({len(raw_data)} bytes) is not a multiple of 2."
            )
            return False

        return True

    def _validate_audio_array(self, audio: np.ndarray)-> bool:
        """Validation stricte des dimensions et du format du tableau NumPy converti."""
        if audio.dtype != np.float32:
            self.logger.log_error(
                f"Invalid audio dtype: expected float32, got {audio.dtype}"
            )
            return False

        if audio.ndim != 1:
            self.logger.log_error(
                f"Invalid audio layout: expected 1D (mono), got shape {audio.shape}"
            )
            return False

        if audio.size == 0:
            self.logger.log_error("Converted audio array is empty.")
            return False

        return True

    @staticmethod
    def _segment_to_srt(i: int, seg: dict[str, Any])-> str:
        """Bloc SRT d'un segment transcrit (numéroté à partir de 1)."""
        start, end = format_timestamp(seg["start"]), format_timestamp(seg["end"])
        return f"{i + 1}\n{start} --> {end}\n{seg['text'].strip()}\n\n"

    def _segment_to_ffmpeg_cmd(self, i: int)-> str:
        """Modèle de commande ffmpeg d'extraction du segment (paramètres @... à remplacer)."""
        output = self.target_path / f"chunk_{i:04d}.m4a" if self.target_path else f"chunk_{i:04d}.m4a"
        return " ".join([
            "ffmpeg", "-y", "-ss", "@audio_start", "-to", "@audio_end", "-i", "@audio_path",
            "-af", "@atempo_or_anull", "-c:a", "aac", "-b:a", "192k", str(output),
        ])

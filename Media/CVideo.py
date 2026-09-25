import io
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from Common.CWhisperModel import CWhisperModel
from Common.EEnums import EActionType, EMediaType
from Common.Helpers.Audio import (
    extract_mono_16khz_pcm_stream,
    get_audio_id3_tags,
    get_audio_tags,
)
from Common.Helpers.Dataframe import data_frame_from_csv, data_frame_to_csv
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
    ) -> None:
        super().__init__(json_path, media_type, action_type)
        self.whisper_model = CWhisperModel(
            logger=self.logger, model_size_or_path="large-v3"
        )
        self.chunk_stream_size: int = 65536

    def read(self) -> None:
        """Lit les flux et métadonnées vidéo et les exporte sous forme de fichiers CSV."""
        if self.is_streaming:
            frame_errors = {}
            stream_buffer = io.BytesIO(self._chunk_input_stream())
            if self.action_type == EActionType.ReadAndCheck:
                frame_errors = self.logger.logElapsedTime(
                    lambda: get_video_corrupted_frames_info(stream_buffer),
                    "Video frames checking from input stream..."
                )

            df = pd.DataFrame([frame_errors] if frame_errors else [])
            data_frame_to_csv(
                df=df,
                target=sys.stdout,
                separator=self.data_separator
            )
        else:
            ensure_ffmpeg_available()
            data = self.process_files_parallel(self._get_video_attributes_fields)
            dfs = self.build_dataframes_by_codec(data)
            self.export_dataframes_to_csv_by_codec(dfs)

    def _get_video_attributes_fields(
        self,
        file_path: str | Path
    ) -> dict[str, list[dict[str, Any]]]:
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
                    frame_errors = self.logger.logElapsedTime(
                        lambda: get_video_corrupted_frames_info(path_obj),
                        f"Video frames checking of {path_obj}..."
                    )
                    if frame_errors:
                        result["frameErrors"] = [common_fields | frame_errors]

                audio_tags = dict(get_audio_tags(path_obj))
                result["videoOther"] = [common_fields | audio_tags]

        except Exception as e:  # noqa: BLE001
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
                return self._edit_audio_fields(file_path, fields_data)  # noqa: B023

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
        self, file_path: str | Path, fields: dict[str, Any]
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

        except Exception as e:  # noqa: BLE001
            self.logger.logError(
                f"Error with {path_obj} : failed to edit tags ({e})"
            )
            return False

    def transcribe(self) -> None:
        """Exécute la transcription audio et la synchronisation."""
        if self.is_streaming:
            data = self._transcribe_video_audio_stream()
            df = pd.DataFrame(data)
            data_frame_to_csv(
                df=df,
                target=sys.stdout,
                separator=self.data_separator,
                fields=self.fieldsByCodec.get("audioTranscription"),
            )
        else:
            data_by_codec = self.process_files_parallel(self.transcribe_video_audio_file)
            dfs = self.build_dataframes_by_codec(
                data_by_codec, log_message="Audio transcription map generated for "
            )
            self.export_dataframes_to_csv_by_codec(dfs)

    def transcribe_video_audio_file(
        self,
        file_path: str | Path
    ) -> dict[str, list[dict[str, Any]]]:
        """Convertit un fichier média en flux PCM via CUtils et délègue la transcription à _transcribe_video_audio_stream."""
        path_obj = Path(file_path).resolve()
        if not (is_video(path_obj) or is_audio(path_obj)):
            return {}

        try:
            self.logger.log(f"Extracting PCM stream from file: {path_obj.name}")

            raw_pcm = extract_mono_16khz_pcm_stream(path_obj)

            if not raw_pcm:
                self.logger.logError(
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
            self.logger.logError(f"Error transcribing {path_obj.name}: {ex}")
            return {}

    def _transcribe_video_audio_stream(
        self,
        stream_input: bytes | None = None,
        source_name: str = "stdin",
    ) -> list[dict[str, Any]]:
        """Lit un flux audio PCM 16-bit 16kHz Mono (depuis stdin ou un paramètre bytes) et exécute Whisper."""
        if stream_input is None:
            stream_input = self._chunk_input_stream()

        if not self._validate_raw_pcm_stream(stream_input):
            return []

        audio = np.frombuffer(stream_input, dtype=np.int16).astype(np.float32) / 32768.0

        if not self._validate_audio_array(audio):
            return []

        segment_data = self.logger.logElapsedTime(
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
                "srt": self._get_data(True, i, segment),
                "ffmpeg_cmd": self._get_data(False, i, segment),
            }
            for i, segment in enumerate(segment_data)
        ]

        self.logger.log(f"Transcription successful for {source_name}")
        return audio_transcription

    def _chunk_input_stream(self) -> bytes:
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

    def _get_data(
        self,
        is_srt: bool,
        i: int,
        seg: dict[str, Any]
    ) -> str:
        self.logger.log(f"Get data of the segment {i} : {seg}")

        if is_srt:
            start = format_timestamp(seg["start"])
            end = format_timestamp(seg["end"])
            text = seg["text"].strip()
            return f"{i + 1}\n{start} --> {end}\n{text}\n\n"

        ffmpeg_cmd : list[str] = [
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
            str(self.target_path / f"chunk_{i:04d}.m4a"),
        ]

        return " ".join(ffmpeg_cmd)

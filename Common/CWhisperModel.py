import platform
from pathlib import Path
from typing import Any

import numpy as np
from Common.CLazy import CLazy
from Common.CLogger import CLogger
from faster_whisper import WhisperModel


class CWhisperModel:
    """Wrapper de :class:`faster_whisper.WhisperModel`."""

    def __init__(
        self,
        logger: CLogger,
        model_size_or_path: str,
        device: str = "auto",
        compute_type: str = "default",
    ) -> None:
        """Initialise le wrapper du modèle Whisper."""
        self.model_size_or_path: str = model_size_or_path
        self.device: str = device
        self.compute_type: str = compute_type
        self.logger = logger
        self.lazy_model: CLazy[WhisperModel | None] = CLazy(self._load_model)

    def _load_model(self) -> WhisperModel | None:
        """Ajuste l'environnement d'exécution et charge le modèle Whisper."""
        if self.device == "auto":
            if platform.system() == "Windows":
                self.device = "cpu"
                if self.compute_type == "default":
                    self.compute_type = "int8"
            else:
                self.device = "cuda"
                if self.compute_type == "default":
                    self.compute_type = "float16"

        self.logger.log(
            f"Loading Whisper model ({self.model_size_or_path}) on device '{self.device}' "
            f"with compute_type '{self.compute_type}'..."
        )

        try:
            return WhisperModel(
                self.model_size_or_path,
                device=self.device,
                compute_type=self.compute_type,
            )
        except Exception as e:  # noqa: BLE001
            self.logger.logError(f"Failed to load Whisper model: {e}")
            return None

    def get_file_transcription_data(
        self,
        audio_input: Path | str | np.ndarray,
        beam_size: int = 10,
        patience: float = 2.0,
        best_of: int = 10,
        temperature: float | list[float] | tuple[float, ...] = (
            0.0,
            0.2,
            0.4,
            0.6,
            0.8,
            1.0,
        ),
        condition_on_previous_text: bool = True,
        compression_ratio_threshold: float = 2.4,
        log_prob_threshold: float = -1.0,
        no_speech_threshold: float = 0.6,
        voice_activity_detection_filter: bool = True,
        voice_activity_detection_parameters: dict | None = None,
        word_timestamps: bool = True,
    ) -> list[dict[str, Any]]:
        """Génère les données de transcription à partir d'un fichier audio ou d'un flux numpy."""

        model_instance = self.lazy_model.value
        if model_instance is None:
            self.logger.logError("WhisperModel is not initialized.")
            return []

        try:
            segments, info = model_instance.transcribe(
                audio_input,
                beam_size=beam_size,
                patience=patience,
                best_of=best_of,
                temperature=temperature,
                condition_on_previous_text=condition_on_previous_text,
                compression_ratio_threshold=compression_ratio_threshold,
                log_prob_threshold=log_prob_threshold,
                no_speech_threshold=no_speech_threshold,
                vad_filter=voice_activity_detection_filter,
                vad_parameters=voice_activity_detection_parameters,
                word_timestamps=word_timestamps,
            )

            # Identification du nom du fichier ou du flux pour le log
            if isinstance(audio_input, (str, Path)):
                source_log = Path(audio_input).name
            else:
                source_log = "stream: numpy array"

            # Log de la langue auto-détectée
            self.logger.log(
                f"Auto-detected language for {source_log}: '{info.language}' "
                f"(confidence: {info.language_probability:.2f})"
            )

            results = []
            for segment in segments:
                segment_dict = {
                    "id": segment.id,
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text,
                    "words": [
                        {
                            "start": w.start,
                            "end": w.end,
                            "word": w.word,
                            "probability": w.probability,
                        }
                        for w in (segment.words or [])
                    ]
                    if word_timestamps
                    else [],
                }
                results.append(segment_dict)

            return results

        except Exception as e:  # noqa: BLE001
            self.logger.logError(f"Error during transcription process: {e}")
            return []

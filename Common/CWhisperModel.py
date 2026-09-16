import platform
from pathlib import Path
from typing import Optional, Tuple, Union, List, Dict, Any

import numpy as np
from faster_whisper import WhisperModel

from Common.CLogger import CLogger
from Common.CLazy import CLazy


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
        self.lazy_model: CLazy[Optional[WhisperModel]] = CLazy(self._load_model)

    def _load_model(self) -> Optional[WhisperModel]:
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
        except Exception as e:
            self.logger.logError(f"Failed to load Whisper model: {e}")
            return None

    def get_file_transcription_data(
        self, 
        audio_input: Union[Path, str, np.ndarray],
        beam_size: int = 10,
        patience: float = 2.0,
        best_of: int = 10,
        temperature: Union[float, List[float], Tuple[float, ...]] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        condition_on_previous_text: bool = True,
        compression_ratio_threshold: float = 2.4,
        log_prob_threshold: float = -1.0,
        no_speech_threshold: float = 0.6,
        voice_activity_detection_filter: bool = True,
        voice_activity_detection_parameters: Optional[dict] = None
    ) -> List[Dict[str, Any]]:
        """
        Transcrit un fichier ou flux audio avec paramètres configurés pour une précision maximale.
        """
        try:
            model = self.lazy_model.value
            if not model:
                self.logger.logError("Whisper model not initialized.")
                return []
        
            if isinstance(audio_input, (Path, str)):
                source_log = str(audio_input)
                audio_target = str(audio_input)
            elif isinstance(audio_input, np.ndarray):
                source_log = "in-memory stream"
                audio_target = audio_input
            else:
                self.logger.logError(f"Unsupported audio input type: {type(audio_input)}")
                return []

            # Paramètres VAD par défaut axés sur la précision
            if voice_activity_detection_parameters is None:
                voice_activity_detection_parameters = {
                    "min_silence_duration_ms": 500,
                    "speech_pad_ms": 200
                }

            segments, info = model.transcribe(
                audio_target, 
                beam_size=beam_size,
                patience=patience,
                best_of=best_of,
                temperature=temperature,
                condition_on_previous_text=condition_on_previous_text,
                compression_ratio_threshold=compression_ratio_threshold,
                log_prob_threshold=log_prob_threshold,
                no_speech_threshold=no_speech_threshold,
                vad_filter=voice_activity_detection_filter,
                vad_parameters=voice_activity_detection_parameters
            )
        
            self.logger.log(
                f"Auto-detected language for {source_log}: '{info.language}' "
                f"(confidence: {info.language_probability:.2f})"
            )
        
            return [
                {
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text
                }
                for segment in segments
            ]
    
        except Exception as ex:
            source_log = str(audio_input) if isinstance(audio_input, (Path, str)) else "in-memory stream"
            self.logger.logError(f"Error transcribing {source_log}: {ex}")
            return []
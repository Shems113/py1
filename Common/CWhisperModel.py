import platform
from pathlib import Path
from typing import Optional

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
        self.max_parallel_worker: int = 5
        self.logger = logger
        self.lazy_model: CLazy[Optional[WhisperModel]] = CLazy(self._load_model)

    def _load_model(self) -> Optional[WhisperModel]:
        """Handles smart environment-based device adjustment and loads the Whisper model."""
        if self.device == "auto":
            if platform.system() == "Windows":
                self.device = "cpu"
                if self.compute_type == "default":
                    self.compute_type = "int8"
                self.max_parallel_worker = 4
            else:
                self.device = "cuda"
                if self.compute_type == "default":
                    self.compute_type = "float16"
                self.max_parallel_worker = 1

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
        file_path_obj: Path,
        beam_size: int = 5,
        voice_activity_detection_filter: bool = False,
        voice_activity_detection_parameters: Optional[dict] = None
    ) -> list[dict]:
        """Transcribes an individual file and returns segments along with info."""
        try:
            model = self.lazy_model.value  # Ensure the model is loaded
            if not model:
                raise RuntimeError("Whisper model not initialized.")
            
            segments, info = model.transcribe(
                str(file_path_obj), 
                beam_size=beam_size, 
                vad_filter=voice_activity_detection_filter,
                vad_parameters=voice_activity_detection_parameters
            )
            
            self.logger.log(
                f"Auto-detected language for {file_path_obj.name}: '{info.language}' "
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
            self.logger.logError(f"Error transcribing {file_path_obj.name}: {ex}")
            return []
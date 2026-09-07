from pathlib import Path
from typing import Optional, Union, Dict, Any, List
from faster_whisper import WhisperModel
import platform
from Common.CUtils import (
    get_file_common_fields,
    get_video_streams,
    is_audio,
    is_text_file,
    is_video,
    format_timestamp
)
from Common.EEnums import EActionType, EMediaType
from Media.CMedia import CMedia
from Common.CLazy import CLazy

class CSubtitle(CMedia):
    def __init__(
        self, json_path: Union[str, Path], action_type: EActionType
    ) -> None:
        super().__init__(json_path, EMediaType.Subtitle, action_type)
        # Loads the Whisper model for transcription tasks, with smart device selection based on the environment.
        self.lazy_whisper_model = CLazy(lambda: self._load_whisper_model("large-v3-turbo", "auto", "default"))

    def read(self) -> None:
            """Lit les flux et métadonnées vidéo et les exporte sous forme de fichiers CSV."""
            data = self.process_files_parallel(self._get_subtitle_attributes_fields)
            dfs = self.build_dataframes_by_codec(data)
            self.export_dataframes_to_csv_by_codec(dfs)
    
    def _get_subtitle_attributes_fields(
            self, file_path: Union[str, Path]
        ) -> Dict[str, List[Dict[str, Any]]]:
            """Extrait les flux, chapitres et métadonnées d'un fichier vidéo/texte."""
            path_obj = Path(file_path)
            self.logger.log(f"Get subtitle attributes fields of {path_obj}")
            result: Dict[str, List[Dict[str, Any]]] = {}
    
            if not (is_text_file(path_obj)):
                return result
    
            try:
                streams = get_video_streams(path_obj)
                common_fields = get_file_common_fields(path_obj)
    
                # Regroupement des flux par type de codec (video, audio, subtitle, etc.)
                for stream in streams:
                    codec_type = stream.get("codec_type")
                    if codec_type:
                        result.setdefault(codec_type, []).append(
                            common_fields | stream
                        )
    
            except Exception as e:
                self.logger.logError(
                    f"Error with {path_obj} : failed to get fields ({e})"
                )
    
            return result      
    def _load_whisper_model(self, model_size: str, device: str, compute_type: str) -> Optional[WhisperModel]:
            """Handles smart environment-based device adjustment and loads the Whisper model."""
            if device == "auto":
                if platform.system() == "Windows":
                    device = "cpu"
                    compute_type = "int8" if compute_type == "default" else compute_type
                else:
                    device = "cuda"
                    compute_type = "float16" if compute_type == "default" else compute_type

            self.logger.log(f"Loading Whisper model ({model_size}) on device '{device}' with compute_type '{compute_type}'...")
        
            try:
                return WhisperModel(model_size, device=device, compute_type=compute_type)
            except Exception as e:
                self.logger.logError(f"Failed to load Whisper model: {e}")
                return None

    def transcribe(self) -> None:
        """
        Runs parallel transcription of targeted videos,generates associated .srt files, and exports metadata DataFrames to CSV.
        """
        self.lazy_whisper_model.value
        data = self.process_files_parallel(self._transcribe_video_audio_file)
           
        # Utilisation de la méthode factorisée pour l'export CSV
        dfs = self.build_dataframes_by_codec(data, log_message="Transcribed ")
        self.export_dataframes_to_csv_by_codec(dfs, isOtherSource=True)
    
    def _transcribe_video_audio_file(self, file_path: Union[str, Path]) -> Dict[str, List[Dict[str, Any]]]:
        """Transcribes an individual video or audio file, generates its .srt file, and extracts its attributes."""
        path_obj = Path(file_path)
        result: Dict[str, List[Dict[str, Any]]] = {}

        if not (is_video(path_obj) or is_audio(path_obj)):
            return result
        
        target_dir = self.target_path
        model = self.lazy_whisper_model.value  # Access the lazily loaded Whisper model

        try:
            if not model:
                raise RuntimeError("Whisper model not initialized.")

            # Transcription with faster-whisper (auto-detects language by default)
            segments, info = model.transcribe(str(path_obj), beam_size=5, vad_filter=True)
            
            # Log the automatically detected language and its confidence score
            self.logger.log(
                f"Auto-detected language for {path_obj.name}: '{info.language}' "
                f"(confidence: {info.language_probability:.2f})"
                )

            # Generate the subtitle file in SRT format within the target directory
            srt_path = target_dir  / f"{path_obj.stem}.srt"
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, segment in enumerate(segments, start=1):
                    start_str = format_timestamp(segment.start)
                    end_str = format_timestamp(segment.end)
                    f.write(f"{i}\n{start_str} --> {end_str}\n{segment.text.strip()}\n\n")
            
            # Extract attributes of the generated SRT file to populate DataFrames
            result = self._get_subtitle_attributes_fields(srt_path)
            self.logger.log(f"Transcription successful: {path_obj.name} -> Saved to {srt_path.name}")
            
        except Exception as ex:
            self.logger.logError(f"Error transcribing {path_obj.name}: {ex}")
                
        return result
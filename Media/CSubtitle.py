from pathlib import Path
from typing import Optional, Union, Dict, Any, List
from faster_whisper import WhisperModel
import platform
import pysrt
import pandas as pd
from Common.CUtils import (
    get_file_common_fields,
    get_video_streams,
    is_audio,
    is_text_file,
    is_video,
    format_timestamp,
    time_to_seconds,
    data_frame_from_csv
)
from Common.EEnums import EActionType, EMediaType
from Media.CMedia import CMedia

class CSubtitle(CMedia):
    def __init__(
        self, json_path: Union[str, Path], action_type: EActionType
    ) -> None:
        super().__init__(json_path, EMediaType.Subtitle, action_type)
        # Loads the Whisper model for transcription tasks, with smart device selection based on the environment.
        self.whisper_model = None
        self.mapping_video_srt_files: Dict[Path, List[Path]] = {}  # Mapping of video files to their corresponding SRT files
        self.tolerance_seconds = 0.2  # Default tolerance for subtitle synchronization checks
        
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
        
    def transcribe(self) -> None:
        """
        Runs parallel transcription of targeted videos, generates associated .srt files, and exports metadata DataFrames to CSV.
        """
        self.whisper_model = self._load_whisper_model("large-v3-turbo")
        data = self.process_files_parallel(self._transcribe_video_audio_file)
        # Utilisation de la méthode factorisée pour l'export CSV
        dfs = self.build_dataframes_by_codec(data, log_message="Transcribed ")
        self.export_dataframes_to_csv_by_codec(dfs, isOtherSource=True)
            
    def transcribeAndCheck(self) -> None:
        """
        Runs parallel transcription of targeted videos and checks the synchronization of generated .srt files against existing ones, exporting the results to CSV.
        """
        self.whisper_model = self._load_whisper_model("large-v3-turbo")
        df = data_frame_from_csv(self.other_source_path, self.data_separator)
        
        self.mapping_video_srt_files = (
            {
                Path(file_path).resolve(): [Path(srt).resolve() for srt in srt_list]
                for file_path, srt_list in df.groupby("file_path")["srt_path"].apply(list).to_dict().items()
                }
            )
        
        data = self.process_files_parallel(self._check_subtitles_synchronization)
        dfs = self.build_dataframes_by_codec(data, log_message="Checked ")
        self.export_dataframes_to_csv_by_codec(dfs)
                
    def _load_whisper_model(self, model_size: str, device: str = "auto", compute_type: str = "default") -> Optional[WhisperModel]:
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

    def _transcribe_video_audio_file(self, file_path: Union[str, Path]) -> Dict[str, List[Dict[str, Any]]]:
        """Transcribes an individual video or audio file, generates its .srt file, and extracts its attributes."""
        path_obj = Path(file_path)
        result: Dict[str, List[Dict[str, Any]]] = {}

        if not (is_video(path_obj) or is_audio(path_obj)):
            return result
        
        target_dir = self.target_path

        try:
            # Transcription with faster-whisper (auto-detects language by default)
            segments, _ = self.logger.logElapsedTime(
                lambda: self._get_video_audio_file_transcription(path_obj),
                f"Processing {path_obj.name} to get transcription data...")
            
            # Generate the subtitle file in SRT format within the target directory
            srt_path = target_dir / f"{path_obj.stem}.srt"
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, segment in enumerate(segments, start=1):
                    start_str = format_timestamp(segment.start)
                    end_str = format_timestamp(segment.end)
                    f.write(f"{i}\n{start_str} --> {end_str}\n{segment.text.strip()}\n\n")
            
            # Extract attributes of the generated SRT file to populate DataFrames
            result = self._get_subtitle_attributes_fields(srt_path)
            result= result | {"subtitleMapping": [{"file_path":str(path_obj), "srt_path": str(srt_path)}]}
            self.logger.log(f"Transcription successful: {path_obj.name} -> Saved to {srt_path.name}")
            
        except Exception as ex:
            self.logger.logError(f"Error transcribing {path_obj.name}: {ex}")
                
        return result

    def _get_video_audio_file_transcription(self, file_path_obj: Path) -> tuple:
        """Transcribes an individual video or audio file and returns segments as a list along with info."""
        model = self.whisper_model  # Access the lazily loaded Whisper model

        try:
            if not model:
                raise RuntimeError("Whisper model not initialized.")
    
            # Transcription with faster-whisper (auto-detects language by default)
            segments, info = model.transcribe(str(file_path_obj), beam_size=5, vad_filter=True)
            
            # Log the automatically detected language and its confidence score
            self.logger.log(
                f"Auto-detected language for {file_path_obj.name}: '{info.language}' "
                f"(confidence: {info.language_probability:.2f})"
            )
            
            return segments, info
    
        except Exception as ex:
            self.logger.logError(f"Error transcribing {file_path_obj.name}: {ex}")
            return [], None
    
    def _check_subtitles_synchronization(self, file_path: Path) -> Dict[str, List[Dict[str, Any]]]:
        file_path_obj = Path(file_path).resolve()
        srt_path_objs = self.mapping_video_srt_files.get(file_path_obj, [])
        result: Dict[str, List[Dict[str, Any]]] = {}

        if not any(srt_path_objs):
            self.logger.logError(f"No corresponding srt file found for {file_path_obj.name}. Skipping synchronization check.")
            return result

        segments, _ = self.logger.logElapsedTime(
            lambda: self._get_video_audio_file_transcription(file_path_obj),
            f"Processing {file_path_obj.name} to get transcription data..."
        )
    
        dicoSrt = {}
        for srt_path_obj in srt_path_objs:
            self.logger.log(f"Opening subtitle file: {srt_path_obj.name}...")
            dicoSrt[srt_path_obj] = pysrt.open(srt_path_obj, encoding="utf-8")

        self.logger.log("--- Synchronization Report ---")
        
        # Construction directe avec une compréhension de liste filtrée
        data = [
            {
                "file_path": str(file_path_obj),
                "srt_path": str(srt_path_obj),
                "codec_type": "subtitle",
                "subtitle_index": sub_index,
                "subtitle_text_sample": f"{sub_text[:30]}..." ,
                "time_difference_seconds": time_diff
            }
            for i, segment in enumerate(segments)
            for srt_path_obj, srts in dicoSrt.items()
            for sub_index, sub_text, seg_start, time_diff in [self._check_subtitle_synchronization(i, segment, srt_path_obj, srts)]
            if sub_index is not None  # Ignore si le fichier SRT est épuisé
        ]
        
        df = pd.DataFrame(data)

        if not df.empty:
            df_drift_detected = df[df["time_difference_seconds"].abs() > self.tolerance_seconds]
            if df_drift_detected.empty:
                self.logger.log("✅ No significant drift detected. Subtitles seem well synchronized!")
            else:
                self.logger.log("⚠️ Drift detected in subtitles. Please review the following discrepancies:")
                self.logger.log(df_drift_detected.to_string(index=False))
        else:
            self.logger.log("No subtitle segments found for synchronization check.")

        result = {"subtitleSynchronization": df.to_dict("records")}

        return result

    def _check_subtitle_synchronization(self, index: int, segment: Any, srt_path_obj: Path, srts: Any) -> tuple:
        whisper_segment_start = segment.get("start") if isinstance(segment, dict) else segment.start
        
        if index >= len(srts):
            self.logger.log(f"[{whisper_segment_start}] SRT #{index}: End of subtitle file {srt_path_obj.name} reached.")
            return None, None, whisper_segment_start, None
        
        current_srt = srts[index]
        current_srt_start = time_to_seconds(current_srt.start)
        time_difference_seconds = current_srt_start - whisper_segment_start

        return current_srt.index, current_srt.text, whisper_segment_start, time_difference_seconds
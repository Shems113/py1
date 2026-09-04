from pathlib import Path
from typing import Union, Dict, Any, List
from faster_whisper import WhisperModel
import pandas as pd
import platform
from Common.CUtils import (
    data_frame_from_csv,
    data_frame_to_csv,
    get_audio_id3_tags,
    get_audio_tags,
    get_file_common_fields,
    ensure_ffmpeg_available,
    get_video_chapters,
    get_video_streams,
    is_audio,
    is_text_file,
    is_video,
    format_timestamp
)
from Common.EEnums import EActionType, EMediaType
from Media.CMedia import CMedia

class CVideo(CMedia):
    def __init__(
        self, json_path: Union[str, Path], action_type: EActionType
    ) -> None:
        super().__init__(json_path, EMediaType.Video, action_type)
        ensure_ffmpeg_available()

    def read(self) -> None:
        """Lit les flux et métadonnées vidéo et les exporte sous forme de fichiers CSV."""
        data = self.process_files_parallel(self._get_video_attributes_fields)
        dfs = self._build_dataframes(data)
        self._export_dataframes_to_csv(dfs)

    def _export_dataframes_to_csv(self, dfs: Dict[str, pd.DataFrame]) -> None:
        """Méthode factorisée pour exporter un dictionnaire de DataFrames en CSV."""
        if not self.target_path:
            self.logger.log(
                "Warning: target_path non défini pour le stockage des CSV."
            )
            return

        target_dir = self.target_path

        for codec_type, df in dfs.items():
            csv_file_path = target_dir / f"{codec_type}Result.csv"

            def export_csv() -> None:
                data_frame_to_csv(
                    df, csv_file_path, self.data_separator, self.fieldsByCodec.get(codec_type)
                )

            self.logger.logElapsedTime(export_csv, f"Display {codec_type}")

    def _build_dataframes(self, data: Dict[str, List[Dict[str, Any]]]) -> Dict[str, pd.DataFrame]:
        """Extrait les attributs des fichiers vidéo et construit les DataFrames correspondants."""

        def build_normalized() -> Dict[str, pd.DataFrame]:
            return {
                codec: pd.json_normalize(dicos) for codec, dicos in data.items()
            }

        return self.logger.logElapsedTime(
            build_normalized, "Build dataframe with video files info"
        )

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
            streams = get_video_streams(path_obj)
            chapters = get_video_chapters(path_obj)
            common_fields = get_file_common_fields(path_obj)

            # Regroupement des flux par type de codec (video, audio, subtitle, etc.)
            for stream in streams:
                codec_type = stream.get("codec_type")
                if codec_type:
                    result.setdefault(codec_type, []).append(
                        common_fields | stream
                    )

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

        data_frame_to_csv(df, self.source_path, self.data_separator, self.fieldsByCodec.get("video"))

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
    
    def _load_whisper_model(self, model_size: str, device: str, compute_type: str) -> bool:
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
            self._whisper_model = WhisperModel(model_size, device=device, compute_type=compute_type)
            return True
        except Exception as e:
            self.logger.logError(f"Failed to load Whisper model: {e}")
            return False

    def transcribe(self) -> None:
        """
        Loads the Whisper model, runs parallel transcription of targeted videos,
        generates associated .srt files, and exports metadata DataFrames to CSV.
        """
        if not self._load_whisper_model("large-v3-turbo", "auto", "default"):
            return
            
        try:
            data = self.process_files_parallel(self._transcribe_video_audio_file)
        finally:
            if hasattr(self, "_whisper_model"):
                delattr(self, "_whisper_model")

        # Utilisation de la méthode factorisée pour l'export CSV
        dfs = self._build_dataframes(data)
        self._export_dataframes_to_csv(dfs)
    
    def _transcribe_video_audio_file(self, file_path: Union[str, Path]) -> Dict[str, List[Dict[str, Any]]]:
        """Transcribes an individual video or audio file, generates its .srt file, and extracts its attributes."""
        path_obj = Path(file_path)
        result: Dict[str, List[Dict[str, Any]]] = {}

        if not (is_video(path_obj) or is_audio(path_obj)):
            return result
        
        target_dir = self.target_path

        try:
            model = getattr(self, "_whisper_model", None)
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
            srt_path = target_dir / f"{path_obj.stem}.srt"
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, segment in enumerate(segments, start=1):
                    start_str = format_timestamp(segment.start)
                    end_str = format_timestamp(segment.end)
                    f.write(f"{i}\n{start_str} --> {end_str}\n{segment.text.strip()}\n\n")
            
            # Extract attributes of the generated SRT file to populate DataFrames
            result = self._get_video_attributes_fields(srt_path)
            self.logger.log(f"Transcription successful: {path_obj.name} -> Saved to {srt_path.name}")
            
        except Exception as ex:
            self.logger.logError(f"Error transcribing {path_obj.name}: {ex}")
                
        return result
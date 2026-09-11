from pathlib import Path
from typing import Union, Dict, Any, List
import pandas as pd
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
    is_video
)
from Common.EEnums import EActionType, EMediaType
from Media.CMedia import CMedia

class CVideo(CMedia):
    def __init__(
        self, 
        json_path: Union[str, Path], 
        action_type: EActionType , 
        media_type: EMediaType = EMediaType.Video
    ) -> None:
        super().__init__(json_path, media_type, action_type)
        ensure_ffmpeg_available()

    def read(self) -> None:
        """Lit les flux et métadonnées vidéo et les exporte sous forme de fichiers CSV."""
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
    
    
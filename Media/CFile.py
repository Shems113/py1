from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Union

import pandas as pd

from Common.CUtils import (
    data_frame_from_csv,
    data_frame_to_csv,
    get_audio_id3_tags,
    get_audio_tags,
    get_file_common_fields,
    get_file_name_extension,
    get_file_name_without_extension,
    get_folder_name,
    is_audio,
    is_video,
    move_media_file,
)
from Common.EEnums import EActionType, EMediaType
from Media.CMedia import CMedia


class CFile(CMedia):
    def __init__(
        self, json_path: Union[str, Path], action_type: EActionType
    ) -> None:
        super().__init__(json_path, EMediaType.File, action_type)

    def read(self) -> None:
        """Lit les fichiers génériques et exporte l'analyse sous forme de fichier CSV."""
        if not self.other_source_path:
            self.logger.log(
                "Warning: other_source_path non défini pour le stockage des CSV."
            )
            return

        other_source_dir = self.other_source_path

        for key, df in self._build_dataframes().items():
            csv_path = other_source_dir / f"{key}.csv"

            def export_csv() -> None:
                data_frame_to_csv(
                    df, csv_path, self.data_separator, self.fields
                )

            self.logger.logElapsedTime(export_csv, f"Display {key}")

    def _build_dataframes(self) -> Dict[str, pd.DataFrame]:
        """Extrait les attributs des fichiers et construit le DataFrame de données."""
        data: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        files = self.get_source_files()
        total = len(files)

        for idx, file_path in enumerate(files, start=1):
            self.logger.log_file_progress(idx, total, file_path)
            attributes = self._get_file_attributes_fields(file_path)
            for codec_type, dicos in attributes.items():
                data[codec_type].extend(dicos)

        def build_normalized() -> Dict[str, pd.DataFrame]:
            return {
                codec: pd.json_normalize(dicos) for codec, dicos in data.items()
            }

        results: Dict[str, pd.DataFrame] = self.logger.logElapsedTime(
            build_normalized, "Build dataframe with files info"
        )

        return {f"{codec_type}Result": df for codec_type, df in results.items()}

    def _get_file_attributes_fields(
        self, file_path: Union[str, Path]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Récupère les métadonnées de base du fichier et le déplace/copie si configuré."""
        path_obj = Path(file_path)
        self.logger.log(f"Get attributes fields of {path_obj}")

        try:
            target_file_path = path_obj

            # Traitement du déplacement ou de la copie si une action est définie
            if self.action_type in (
                EActionType.ReadAndMove,
                EActionType.ReadAndCopy,
            ):
                if not self.target_path:
                    raise ValueError(
                        "target_path non défini pour le déplacement/copie"
                    )

                is_move = self.action_type == EActionType.ReadAndMove
                extension = get_file_name_extension(path_obj)
                file_info = get_file_common_fields(path_obj, True)

                target_dir = self.target_path / extension.replace(".", "")
                file_name_without_ext = get_file_name_without_extension(path_obj)

                def file_name_func(idx: int, suffix: str) -> str:
                    index_str = str(idx) if idx != 0 else ""
                    return str(
                        target_dir
                        / f"{file_name_without_ext}{index_str}{extension}"
                    )

                def is_same_media_func(other_file: Union[str, Path]) -> bool:
                    other_path = Path(other_file)
                    return (
                        other_path.is_file()
                        and file_info == get_file_common_fields(other_path, True)
                    )

                move_result = move_media_file(
                    path_obj,
                    target_dir,
                    file_name_func,
                    is_same_media_func,
                    index=0,
                    is_move=is_move,
                )

                target_file_path = Path(move_result[0])
                if move_result[1]:
                    action_str = "Move" if is_move else "Copy"
                    self.logger.log(
                        f"{action_str} {path_obj} to {target_file_path}"
                    )

            return {"file": [get_file_common_fields(target_file_path, True)]}

        except Exception as e:
            self.logger.log(
                f"Error with {path_obj} : failed to get fields ({e})"
            )
            return {}

    def write(self) -> None:
        """Met à jour les étiquettes des fichiers à partir de la source CSV."""
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

        data_frame_to_csv(df, self.source_path, self.data_separator, self.fields)

    def _edit_audio_fields(
        self, file_path: Union[str, Path], fields: Dict[str, Any]
    ) -> bool:
        """Modifie les étiquettes si le fichier s'avère être du contenu multimédia (Audio/Vidéo)."""
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
            self.logger.log(
                f"Error with {path_obj} : failed to edit tags ({e})"
            )
            return False
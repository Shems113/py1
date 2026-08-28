from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Union

import pandas as pd

from Common.CUtils import (
    add_audio_id3_images,
    clean_file_name,
    clean_last_directory_name,
    data_frame_from_csv,
    data_frame_to_csv,
    get_audio_id3_tags,
    get_file_common_fields,
    get_file_name_extension,
    is_audio,
    move_media_file,
)
from Common.EEnums import EActionType, EMediaType
from Media.CMedia import CMedia


class CMusic(CMedia):
    def __init__(
        self, json_path: Union[str, Path], action_type: EActionType
    ) -> None:
        super().__init__(json_path, EMediaType.Music, action_type)

    def read(self) -> None:
        """Lit les fichiers audio sources et exporte les analyses en CSV."""
        if not self.other_source_path:
            self.logger.log(
                "Warning: other_source_path non défini pour le stockage des CSV."
            )
            return

        other_source = self.other_source_path

        for key, df in self._build_dataframes().items():
            target_csv = other_source / f"{key}.csv"

            def export_csv() -> None:
                data_frame_to_csv(
                    df, target_csv, self.data_separator)

            self.logger.logElapsedTime(export_csv, f"Display {key}")

        self.remove_empty_folders()

    def _build_dataframes(self) -> Dict[str, pd.DataFrame]:
        """Extrait les métadonnées de la musique et construit les DataFrames de données et d'agrégation."""
        data: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        files = self.get_source_files()
        total = len(files)

        # 1. Extraction et stockage des attributs
        for idx, file_path in enumerate(files, start=1):
            self.logger.log_file_progress(idx, total, file_path)
            attributes = self._get_music_attributes_fields(file_path)
            for codec_type, dicos in attributes.items():
                data[codec_type].extend(dicos)

        # 2. Normalisation Pandas
        def build_normalized() -> Dict[str, pd.DataFrame]:
            return {
                codec: pd.json_normalize(dicos)
                for codec, dicos in data.items()
            }

        raw_results: Dict[str, pd.DataFrame] = self.logger.logElapsedTime(
            build_normalized, "Build dataframe with music files info"
        )

        # 3. Agrégation des statistiques par colonne
        def build_aggregated() -> Dict[str, pd.DataFrame]:
            aggregated = {}
            for codec, df in raw_results.items():
                if df.empty:
                    aggregated[codec] = pd.DataFrame()
                    continue

                agg_dfs = []
                for col in df.columns:
                    if col == "path":
                        continue

                    counts = (
                        df.value_counts(col)
                        .to_frame("count")
                        .reset_index()
                        .assign(key=col)
                        .rename(columns={col: "value"})[["key", "value", "count"]]
                    )
                    agg_dfs.append(counts)

                aggregated[codec] = (
                    pd.concat(agg_dfs, ignore_index=True)
                    if agg_dfs
                    else pd.DataFrame()
                )
            return aggregated

        agg_results: Dict[str, pd.DataFrame] = self.logger.logElapsedTime(
            build_aggregated,
            "Build aggregated dataframe with music files info",
        )

        # 4. Fusion des dictionnaires de résultats
        final_dfs = {}
        for codec_type, df in raw_results.items():
            final_dfs[f"{codec_type}Result"] = df
        for codec_type, df in agg_results.items():
            final_dfs[f"{codec_type}ResultAgg"] = df

        return final_dfs

    def _get_music_attributes_fields(
        self, file_path: Union[str, Path]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Extrait les tags et déplace/copie le fichier si configuré."""
        path_obj = Path(file_path)
        self.logger.log(f"Get music attributes fields of {path_obj}")

        if not is_audio(path_obj):
            return {}

        try:
            audio = get_audio_id3_tags(path_obj)
            target_file_path = path_obj

            # Traitement du déplacement ou de la copie si demandé
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

                artist = audio.get("albumartist", ["UNKNOWN"])[0]
                default_album = self.item.get("AlbumDefaultName", "{}").format(
                    artist
                )
                album = audio.get("album", [default_album])[0]
                index = audio.get("tracknumber", [1])[0]

                target_dir = (
                    self.target_path
                    / extension.replace(".", "")
                    / clean_last_directory_name(artist.upper())
                    / clean_last_directory_name(album.upper())
                )

                def file_name_func(idx: int, suffix: str) -> str:
                    prefix = f"{idx}{suffix}" if idx != 0 else ""
                    title = clean_file_name(audio.get("title", [""])[0])
                    return str(target_dir / f"{prefix}{title}{extension}")

                def is_same_media_func(other_file: Union[str, Path]) -> bool:
                    other_path = Path(other_file)
                    if not other_path.is_file():
                        return False
                    other_audio = get_audio_id3_tags(other_path)
                    return all(
                        audio.get(k) == other_audio.get(k) for k in audio
                    )

                move_result = move_media_file(
                    path_obj,
                    target_dir,
                    file_name_func,
                    is_same_media_func,
                    index,
                    is_move,
                )

                target_file_path = Path(move_result[0])
                if move_result[1]:
                    action_str = "Move" if is_move else "Copy"
                    self.logger.log(
                        f"{action_str} {path_obj} to {target_file_path}"
                    )

            common_fields = get_file_common_fields(target_file_path, False)

            # Formatage des données de tags
            music_tags = {k: "|".join(map(str, v)) for k, v in audio.items()}
            other_info = vars(audio.info) if hasattr(audio, "info") else {}

            return {
                "music": [common_fields | music_tags],
                "musicOther": [common_fields | other_info],
            }

        except Exception as e:
            self.logger.logError(
                f"Error with {file_path} : failed to get fields ({e})"
            )
            return {}

    def write(self) -> None:
        """Met à jour les tags texte et images des fichiers audio."""
        self._write_audio_fields()
        self._write_audio_images()

    def _write_audio_fields(self) -> None:
        """Édite les métadonnées texte ID3 à partir de la source CSV."""
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

        data_frame_to_csv(df, self.source_path, self.data_separator)

    def _edit_audio_fields(
        self, file_path: Union[str, Path], fields: Dict[str, Any]
    ) -> bool:
        """Modifie les champs ID3 d'un fichier audio spécifique."""
        path_obj = Path(file_path)
        if not is_audio(path_obj):
            return False

        try:
            audio = get_audio_id3_tags(path_obj)
            updates = {}

            for key, value in fields.items():
                current_val = audio.get(key, [None])[0]
                str_value = str(value)
                if str_value != current_val:
                    updates[key] = str_value.split("@")

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

    def _write_audio_images(self) -> None:
        """Insère les pochettes/images dans les métadonnées ID3 des fichiers audio."""
        if not self.other_source_path or not self.other_source_path.is_file():
            return

        other_source = self.other_source_path
        df = data_frame_from_csv(other_source, self.data_separator)

        if df.empty:
            return

        # Restructuration des images par fichier
        image_mapping: Dict[
            str, Dict[str, Dict[str, str]]
        ] = defaultdict(lambda: defaultdict(dict))
        for row in df.itertuples(index=False):
            image_mapping[row.path][row.imagePath][row.imageType] = (
                row.imageDescription
            )

        paths_to_remove = []

        for file_path, images_config in image_mapping.items():
            path_obj = Path(file_path)
            if not is_audio(path_obj):
                continue

            def add_images_task() -> None:
                add_audio_id3_images(path_obj, images_config)

            try:
                self.logger.logElapsedTime(
                    add_images_task,
                    f"Add images to {path_obj} with {images_config}",
                )
                paths_to_remove.append(file_path)
            except Exception as e:
                self.logger.log(
                    f"Error with {path_obj} : failed to add image {images_config} ({e})"
                )

        if paths_to_remove:
            df = df[~df["path"].isin(paths_to_remove)]

        data_frame_to_csv(df, other_source, self.data_separator)
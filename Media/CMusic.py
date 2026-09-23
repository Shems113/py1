import typing
from collections import defaultdict
from pathlib import Path

import pandas as pd
from Common.EEnums import EActionType, EMediaType
from Common.Helpers.Audio import (
    add_audio_id3_images,
    get_audio_id3_tags,
)
from Common.Helpers.Dataframe import data_frame_from_csv, data_frame_to_csv
from Common.Helpers.File import (
    clean_file_name,
    clean_last_directory_name,
    get_file_common_fields,
    get_file_name_extension,
)
from Common.Helpers.Media import (
    is_audio,
    move_media_file,
)
from Media.CMedia import CMedia


class CMusic(CMedia):
    def __init__(
        self,
        json_path: str | Path,
        action_type: EActionType
    ) -> None:
        super().__init__(json_path, EMediaType.Music, action_type)

    def read(self) -> None:
        """Lit les fichiers audio sources et exporte les analyses en CSV."""
        if not self.other_source_path:
            self.logger.log(
                "Warning: other_source_path non défini pour le stockage des CSV."
            )
            return

        data = self.process_files_parallel(self._get_music_attributes_fields)
        dfs = self._build_dataframes(data)
        self.export_dataframes_to_csv_by_codec(dfs, isOtherSource=True)
        self.remove_empty_folders()

    def _build_dataframes(
        self,
        data: dict[str, list[dict[str, typing.Any]]]
        ) -> dict[str, pd.DataFrame]:
        # Normalisation Pandas & construction des DataFrames (raw et agrégé)
        raw_results: dict[str, pd.DataFrame] = self.build_dataframes_by_codec(data)
        agg_results: dict[str, pd.DataFrame] = self.build_dataframes_by_codec(self._build_aggregated(raw_results), log_message="Aggregated ")
        return raw_results | {f"{codec_type}Agg": df for codec_type, df in agg_results.items()}

    def _build_aggregated(
        self,
        data: dict[str, pd.DataFrame]
        ) -> dict[str, pd.DataFrame]:
        aggregated = {}
        for codec, df in data.items():
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


    def _get_music_attributes_fields(
        self,
        file_path: str | Path
    ) -> dict[str, list[dict[str, typing.Any]]]:
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

                def is_same_media_func(other_file: str | Path) -> bool:
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

        except Exception as e:  # noqa: BLE001
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
                return self._edit_audio_fields(file_path, fields_data)  # noqa: B023

            log_msg = f"Edit {file_path} with fields {media_fields}"
            if self.logger.logElapsedTime(edit_task, log_msg):
                rows_to_drop.append(row.Index)

        if rows_to_drop:
            df = df.drop(index=rows_to_drop)

        data_frame_to_csv(df, self.source_path, self.data_separator)

    def _edit_audio_fields(
        self,
        file_path: str | Path,
        fields: dict[str, typing.Any]
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

        except Exception as e:  # noqa: BLE001
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
        image_mapping: dict[
            str, dict[str, dict[str, str]]
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
                add_audio_id3_images(path_obj, images_config)  # noqa: B023

            try:
                self.logger.logElapsedTime(
                    add_images_task,
                    f"Add images to {path_obj} with {images_config}",
                )
                paths_to_remove.append(file_path)
            except Exception as e:  # noqa: BLE001
                self.logger.log(
                    f"Error with {path_obj} : failed to add image {images_config} ({e})"
                )

        if paths_to_remove:
            df = df[~df["path"].isin(paths_to_remove)]

        data_frame_to_csv(df, other_source, self.data_separator)

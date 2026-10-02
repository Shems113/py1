import fnmatch
import functools
import os
import sys
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from Common.CLogger import CLogger as Logger
from Common.EEnums import EActionType, EMediaType
from Common.Helpers.Audio import get_audio_id3_tags, get_audio_tags
from Common.Helpers.Dataframe import (
    data_frame_column_hashable,
    data_frame_from_csv,
    data_frame_to_csv,
)
from Common.Helpers.File import remove_empty_folders, resolve_project_path
from Common.Helpers.Json import json_from_file
from Common.Helpers.Media import is_audio, is_video, move_media_file
from Common.Helpers.System import CPU_COUNT, IS_WINDOWS

FileAttributes = dict[str, list[dict[str, Any]]]


class CMedia:
    def __init__(
        self,
        json_path: str | Path,
        media_type: EMediaType,
        action_type: EActionType
        )-> None:
        self.json_path = json_path
        self.media_type = media_type
        self.action_type = action_type
        # stdin redirigé (pipe SSH) ; sys.stdin vaut None sous pythonw
        self.is_streaming = sys.stdin is not None and not sys.stdin.isatty()
        self.data_separator: str = ";"

        # Chargement de la configuration
        self.item: dict[str, Any] = self._load_item(Path(self.json_path))

        # Chemins regroupés dans « Paths » : absolus tels quels, relatifs résolus depuis la
        # racine du projet (RootSourcePaths / ExtraFileRootSourcePaths : chemins alternatifs,
        # non lus par le code, gardés pour basculer facilement)
        paths: dict[str, str] = self.item.get("Paths", {})
        self.source_path: Path = resolve_project_path(paths.get("RootSourcePath"))
        extra_path = paths.get("ExtraFileRootSourcePath")
        self.other_source_path: Path | None = resolve_project_path(extra_path) if extra_path else None
        target_path = paths.get("RootTargetPath")
        self.target_path: Path | None = resolve_project_path(target_path) if target_path else None
        log_root = resolve_project_path(paths.get("LogRootSourcePath", "logs"))

        # Nom du log daté en AAAA-MM-JJ : les fichiers se trient chronologiquement
        date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
        self.log_paths: list[str] = [str(log_root / f"{date_str}_{media_type.name}_{action_type.name}.txt")]

        self.file_patterns: list[str] = self.item.get("FilePatterns", ["*"])
        self.fields_by_codec: dict[str, list[str]] = self.item.get("Fields", {})

        # Création automatique de tous les répertoires s'ils n'existent pas
        log_root.mkdir(parents=True, exist_ok=True)
        if self.target_path and not self.target_path.suffix:
            self.target_path.mkdir(parents=True, exist_ok=True)

        self.max_worker = CPU_COUNT
        self.logger = Logger(self.log_paths)

    def _load_item(
        self,
        json_path: Path
        )-> dict[str, Any]:
        """Extrait l'élément de configuration correspondant au MediaType et à l'Action."""
        media_name = self.media_type.name
        action_name = self.action_type.name
        items = json_from_file(json_path).get(media_name, [])

        # Action exacte d'abord, sinon action de base (ex. « ReadAndMove » -> « Read »)
        target_item = next((e for e in items if e.get("Action") == action_name), None) or next(
            (e for e in items if e.get("Action") and action_name.startswith(e["Action"])), None)

        if not target_item:
            raise ValueError(
                f"Aucun élément trouvé dans {json_path} pour "
                f"MediaType='{media_name}' et Action='{action_name}'"
            )
        return target_item

    def get_source_files(self)-> list[str]:
        """Fichiers sources correspondant aux motifs, triés : UN seul parcours de l'arborescence,
        quel que soit le nombre de motifs (auparavant un parcours complet par motif)."""
        def fetch_files()-> list[str]:
            if not self.source_path.is_dir():
                return []
            # Motifs insensibles à la casse sous Windows, comme l'explorateur
            patterns = [p.lower() for p in self.file_patterns] if IS_WINDOWS else self.file_patterns
            matched = []
            for root, _dirs, files in os.walk(self.source_path):
                for name in files:
                    key = name.lower() if IS_WINDOWS else name
                    if any(fnmatch.fnmatchcase(key, p) for p in patterns):
                        matched.append(str(Path(root, name).resolve()))
            return sorted(matched)

        return self.logger.log_elapsed_time(fetch_files, "Get source files")

    def init_log(self)-> None:
        """Initialise le fichier de log avec les métadonnées de l'opération."""
        for entry in (
            f"Media : {self.media_type.name}",
            f"Action : {self.action_type.name}",
            f"Is streaming : {self.is_streaming}",
            f"Log paths : {self.log_paths}",
            f"Source directory path : {self.source_path}",
            f"File patterns : {self.file_patterns}",
            f"Target directory path : {self.target_path}",
        ):
            self.logger.log(entry)

    def remove_empty_folders(self)-> None:
        """Suppression récursive des dossiers vides dans le répertoire source."""
        remove_empty_folders(self.source_path)

    def process_files_parallel(
        self,
        extract_func: Callable[[str], FileAttributes]
    )-> FileAttributes:
        """Exécute l'extraction d'attributs de fichiers en parallèle et accumule les résultats."""
        data: FileAttributes = defaultdict(list)
        files = self.get_source_files()
        total = len(files)

        def worker(idx: int, file_path: str)-> FileAttributes:
            self.logger.log_file_progress(idx, total, file_path)
            return extract_func(file_path)

        with ThreadPoolExecutor(max_workers=self.max_worker) as executor:
            futures = [executor.submit(worker, idx, f) for idx, f in enumerate(files, start=1)]
            for future in as_completed(futures):
                for codec_type, dicos in future.result().items():
                    data[codec_type].extend(dicos)

        return data

    # ------------------------------------------------------------------ #
    # Déplacement / copie (ReadAndMove, ReadAndCopy)
    # ------------------------------------------------------------------ #
    @property
    def is_move_or_copy(self)-> bool:
        return self.action_type in (EActionType.ReadAndMove, EActionType.ReadAndCopy)

    def _move_or_copy(
        self,
        path_obj: Path,
        target_dir: Path,
        file_name_func: Callable[[int, str], str],
        is_same_media_func: Callable[[str | Path], bool],
        default_index: int = 0,
    )-> Path:
        """Déplace ou copie le fichier dans target_dir (collisions gérées par move_media_file)
        et renvoie son nouveau chemin."""
        is_move = self.action_type == EActionType.ReadAndMove
        new_path, done = move_media_file(
            path_obj, target_dir, file_name_func, is_same_media_func, default_index, is_move)
        if done:
            self.logger.log(f"{'Move' if is_move else 'Copy'} {path_obj} to {new_path}")
        return Path(new_path)

    # ------------------------------------------------------------------ #
    # Écriture de tags depuis un CSV (action Write)
    # ------------------------------------------------------------------ #
    def _write_tags_from_csv(
        self,
        fields_key: str | None = None,
        allow_video: bool = True,
    )-> None:
        """Applique les tags du CSV source (colonne path + une colonne par tag) aux fichiers,
        puis réécrit le CSV sans les lignes traitées avec succès."""
        if not self.source_path.is_file():
            return

        df = data_frame_from_csv(self.source_path, self.data_separator)
        if df.empty or "path" not in df.columns:
            return

        media_fields = [col for col in df.columns if col != "path"]
        rows_to_drop = []

        for row in df.itertuples():
            fields_data = {col: getattr(row, col) for col in media_fields if pd.notna(getattr(row, col))}
            done = self.logger.log_elapsed_time(
                functools.partial(self._edit_media_tags, row.path, fields_data, allow_video),
                f"Edit {row.path} with fields {media_fields}",
            )
            if done:
                rows_to_drop.append(row.Index)

        data_frame_to_csv(
            df.drop(index=rows_to_drop),
            self.source_path,
            self.data_separator,
            self.fields_by_codec.get(fields_key) if fields_key else None,
        )

    def _edit_media_tags(
        self,
        file_path: str | Path,
        fields: dict[str, Any],
        allow_video: bool = True,
    )-> bool:
        """Modifie les tags d'un fichier audio (ou vidéo si allow_video). Plusieurs valeurs : a@b."""
        path_obj = Path(file_path)
        audio_file = is_audio(path_obj)
        if not (audio_file or (allow_video and is_video(path_obj))):
            return False

        try:
            tags = get_audio_id3_tags(path_obj) if audio_file else get_audio_tags(path_obj)
            if tags is None:
                raise ValueError("format non reconnu par mutagen")

            updates = {
                key: str(value).split("@")
                for key, value in fields.items()
                if str(value) != tags.get(key, [None])[0]
            }
            if updates:
                tags.update(updates)
                tags.save(path_obj)
                self.logger.log(f"Update {path_obj} tags with {updates}")
            return True

        except Exception as e:  # noqa: BLE001
            self.logger.log_error(f"Error with {path_obj} : failed to edit tags ({e})")
            return False

    # ------------------------------------------------------------------ #
    # DataFrames et export CSV
    # ------------------------------------------------------------------ #
    def build_dataframes_by_codec(
        self,
        data: FileAttributes | dict[str, pd.DataFrame],
        log_message: str = "",
        skip_aggregation: bool = False
    )-> dict[str, pd.DataFrame]:
        """Construit un DataFrame par type de données, et son agrégat (suffixe Agg)."""
        raw_results: dict[str, pd.DataFrame] = self.logger.log_elapsed_time(
            lambda: {codec_type: values if isinstance(values, pd.DataFrame) else pd.json_normalize(values)
                     for codec_type, values in data.items()},
            f"Build dataframe with {log_message}{self.media_type.name} files info"
        )
        if skip_aggregation:
            return raw_results

        agg_results: dict[str, pd.DataFrame] = self.logger.log_elapsed_time(
            lambda: self._build_aggregated_dataframes_by_codec(raw_results),
            f"Build aggregated dataframe with {log_message}{self.media_type.name} files info"
        )
        return raw_results | {f"{codec_type}Agg": df for codec_type, df in agg_results.items()}

    def _build_aggregated_dataframes_by_codec(
        self,
        data: dict[str, pd.DataFrame]
        )-> dict[str, pd.DataFrame]:
        """Pour chaque colonne (sauf path) : nombre d'occurrences de chaque valeur."""
        aggregated = {}
        for codec, df in data.items():
            agg_dfs = [
                data_frame_column_hashable(df[col]).value_counts()
                .to_frame("count")
                .reset_index()
                .assign(key=col)
                .rename(columns={col: "value"})[["key", "value", "count"]]
                for col in df.columns
                if col != "path"
            ]
            aggregated[codec] = pd.concat(agg_dfs, ignore_index=True) if agg_dfs else pd.DataFrame()
        return aggregated

    def export_dataframes_to_csv_by_codec(
        self,
        dataframes: dict[str, pd.DataFrame],
        is_other_source: bool = False
    )-> None:
        """Exporte chaque DataFrame en <type>Result.csv, dans target_path ou other_source_path."""
        target_dir = self.other_source_path if is_other_source else self.target_path
        if not target_dir:
            name = "other_source_path" if is_other_source else "target_path"
            self.logger.log(f"Warning: {name} non défini pour le stockage des CSV.")
            return

        for codec_type, df in dataframes.items():
            csv_file_path = target_dir / f"{codec_type}Result.csv"
            self.logger.log_elapsed_time(
                functools.partial(
                    data_frame_to_csv, df, csv_file_path, self.data_separator, self.fields_by_codec.get(codec_type)),
                f"Display {codec_type}",
            )

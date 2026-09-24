import sys
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from Common.CLogger import CLogger as Logger
from Common.Helpers.Dataframe import data_frame_to_csv
from Common.Helpers.File import remove_empty_folders
from Common.Helpers.Json import json_from_file
from Common.Helpers.System import CPU_COUNT


class CMedia:
    def __init__(
        self,
        json_path: str | Path,
        media_type: Any,
        action_type: Any
    ) -> None:
        self.json_path = json_path
        self.media_type = media_type
        self.action_type = action_type
        self.is_streaming = not sys.stdin.isatty()
        self.data_separator: str = ";"

        # Détermination dynamique de la racine du projet
        self.project_root: Path = Path(__file__).resolve().parents[1]

        # Chargement de la configuration
        self.item: dict[str, Any] = self._load_item(Path(self.json_path))

        # Résolution universelle et générique des chemins
        self.source_path: Path = self._resolve_path(self.item.get("RootSourcePath"))

        extra_path = self.item.get("ExtraFileRootSourcePath")
        self.other_source_path: Path | None = self._resolve_path(extra_path) if extra_path else None

        target_path = self.item.get("RootTargetPath")
        self.target_path: Path | None = self._resolve_path(target_path) if target_path else None

        log_root = self._resolve_path(self.item.get("LogRootSourcePath", "logs"))

        # Construction du chemin de log
        date_str = datetime.now().astimezone().strftime("%d-%m-%Y")
        log_filename = f"{date_str}_{self.media_type.name}_{self.action_type.name}.txt"
        self.log_paths: list[str] = [str(log_root / log_filename)]

        self.file_patterns: list[str] = self.item.get("FilePatterns", ["*"])
        self.fieldsByCodec: Any = self.item.get("Fields", {})

        # Création automatique de tous les répertoires s'ils n'existent pas
        log_root.mkdir(parents=True, exist_ok=True)
        if self.target_path and not self.target_path.suffix:
            self.target_path.mkdir(parents=True, exist_ok=True)

        self.max_worker = CPU_COUNT
        self.logger = Logger(self.log_paths)

    def _resolve_path(
        self,
        raw_path: str | Path | None
        ) -> Path:
        """Résout un chemin de manière générique : absolu s'il l'est, sinon relatif à la racine du projet."""
        if not raw_path:
            return self.project_root

        path_obj = Path(str(raw_path))
        if path_obj.is_absolute():
            return path_obj

        return self.project_root / path_obj

    def _load_item(
        self,
        json_path: Path
        ) -> dict[str, Any]:
        """Extrait l'élément de configuration correspondant au MediaType et à l'Action."""
        media_name = self.media_type.name
        action_name = self.action_type.name

        data = json_from_file(json_path)
        items = data.get(media_name, [])

        target_item = next(
            (
                e for e in items if action_name == e.get("Action", "")
            ),
            None,
        )

        if target_item is None:
            target_item = next(
                (
                    e for e in items if action_name.startswith(e.get("Action", ""))
                ),
                None,
            )

        if not target_item:
            raise ValueError(
                f"Aucun élément trouvé dans {json_path} pour "
                f"MediaType='{media_name}' et Action='{action_name}'"
            )

        return target_item

    def get_source_files(self) -> list[str]:
        """Recherche les fichiers sources correspondant aux motifs définis de manière optimisée."""
        def fetch_files() -> list[str]:
            if not self.source_path.exists():
                return []

            matched_files = {
                str(p) for pattern in self.file_patterns
                for p in self.source_path.rglob(pattern) if p.is_file()
            }
            return list(matched_files)

        return self.logger.logElapsedTime(fetch_files, "Get source files")

    def init_log(self) -> None:
        """Initialise le fichier de log avec les métadonnées de l'opération."""
        logs = (
            f"Media : {self.media_type.name}",
            f"Action : {self.action_type.name}",
            f"Is streaming : {self.is_streaming}",
            f"Log paths : {self.log_paths}",
            f"Source directory path : {self.source_path}",
            f"File patterns : {self.file_patterns}",
            f"Target directory path : {self.target_path}"
        )
        for entry in logs:
            self.logger.log(entry)

    def remove_empty_folders(self) -> None:
        """Suppression récursive des dossiers vides dans le répertoire source."""
        remove_empty_folders(self.source_path)

    def process_files_parallel(
        self,
        extract_func: Callable[[str], dict[str, list[dict[str, Any]]]]
    ) -> dict[str, list[dict[str, Any]]]:
        """Exécute l'extraction d'attributs de fichiers en parallèle et accumule les résultats."""
        data: dict[str, list[dict[str, Any]]] = defaultdict(list)
        files = self.get_source_files()
        total = len(files)

        if not files:
            return data

        def worker(idx: int, file_path: str):
            self.logger.log_file_progress(idx, total, file_path)
            return extract_func(file_path)

        with ThreadPoolExecutor(max_workers=self.max_worker) as executor:
            futures = [
                executor.submit(worker, idx, file_path)
                for idx, file_path in enumerate(files, start=1)
            ]

            for future in as_completed(futures):
                attributes = future.result()
                for codec_type, dicos in attributes.items():
                    data[codec_type].extend(dicos)

        return data

    def build_dataframes_by_codec(
        self,
        data: dict[str, list[dict[str, Any]]],
        log_message: str=""
    ) -> dict[str, pd.DataFrame]:
        """Extrait les attributs des fichiers et construit le DataFrame de données."""

        def build_normalized() -> dict[str, pd.DataFrame]:
            return (
                {codec_type: (value if isinstance(value, pd.DataFrame) else pd.json_normalize(value))
                for codec_type, value in data.items()}
                )

        return self.logger.logElapsedTime(
            build_normalized, f"Build dataframe with {log_message}{self.media_type.name} files info"
        )

    def export_dataframes_to_csv_by_codec(
        self,
        dicoOfDataframes: dict[str, pd.DataFrame],
        isOtherSource: bool=False
    ) -> None:
        """Méthode factorisée pour exporter un dictionnaire de DataFrames en CSV."""
        if not self.target_path:
            self.logger.log(
                "Warning: target_path non défini pour le stockage des CSV."
            )
            return

        target_dir = self.target_path if not isOtherSource else self.other_source_path

        for codec_type, df in dicoOfDataframes.items():
            csv_file_path = target_dir / f"{codec_type}Result.csv"

            def export_csv() -> None:
                data_frame_to_csv(
                    df, csv_file_path, self.data_separator, self.fieldsByCodec.get(codec_type)  # noqa: B023
                )

            self.logger.logElapsedTime(export_csv, f"Display {codec_type}")

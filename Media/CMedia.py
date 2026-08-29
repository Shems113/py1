from datetime import datetime
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Union, Callable

import Common.CUtils as Utils
from Common.CLogger import CLogger as Logger

class CMedia:
    def __init__(
        self, json_path: Union[str, Path], media_type: Any, action_type: Any
    ) -> None:
        self.media_type = media_type
        self.action_type = action_type
        self.data_separator: str = ";"

        # Extraction optimisée de la configuration
        json_file_path = Path(json_path)
        self.item: Dict[str, Any] = self._load_item(json_file_path)

        # Construction du chemin de log
        date_str = datetime.now().strftime("%d-%m-%Y")
        log_filename = f"{date_str}_{self.media_type.name}_{self.action_type.name}.txt"
        log_root = Path(self.item["LogRootSourcePath"])
        self.log_paths: List[str] = [str(log_root / log_filename)]

        # Propriétés de structure (Pathlib native)
        self.source_path: Path = Path(self.item["RootSourcePath"])
        self.other_source_path: Optional[Path] = (
            Path(self.item["ExtraFileRootSourcePath"])
            if self.item.get("ExtraFileRootSourcePath")
            else None
        )
        self.file_patterns: List[str] = self.item.get("FilePatterns", ["*"])
        self.target_path: Optional[Path] = (
            Path(self.item["RootTargetPath"])
            if self.item.get("RootTargetPath")
            else None
        )
        self.fieldsByCodec: Optional[Any] = self.item.get("Fields", {})

        self.logger = Logger(self.log_paths)

    def _load_item(self, json_path: Path) -> Dict[str, Any]:
        """Extrait l'élément de configuration correspondant au MediaType et à l'Action."""
        media_name = self.media_type.name
        action_name = self.action_type.name

        data = Utils.json_from_file(json_path)

        target_item = next(
            (
                elem
                for elem in data
                if elem.get("MediaType") == media_name
                and action_name.startswith(elem.get("Action", ""))
            ),
            None,
        )

        if not target_item:
            raise ValueError(
                f"Aucun élément trouvé dans {json_path} pour "
                f"MediaType='{media_name}' et Action='{action_name}'"
            )

        return target_item

    def get_source_files(self) -> List[str]:
        """Recherche les fichiers sources correspondant aux motifs définis."""

        def fetch_files() -> List[str]:
            if not self.source_path.exists():
                return []
            matched_files: set[Path] = set()
            for pattern in self.file_patterns:
                matched_files.update(
                    p for p in self.source_path.rglob(pattern) if p.is_file()
                )
            return [str(p) for p in matched_files]

        return self.logger.logElapsedTime(fetch_files, "Get source files")

    def init_log(self) -> None:
        """Initialise le fichier de log avec les métadonnées de l'opération."""
        logs = (
            f"Media : {self.media_type.name}",
            f"Action : {self.action_type.name}",
            f"Log paths : {self.log_paths}",
            f"Source directory path : {self.source_path}",
            f"File patterns : {self.file_patterns}",
            f"Target directory path : {self.target_path}",
        )
        for entry in logs:
            self.logger.log(entry)

    def remove_empty_folders(self) -> None:
        """Suppression récursive des dossiers vides dans le répertoire source."""
        Utils.remove_empty_folders(self.source_path)

    def process_files_parallel(
        self, extract_func: Callable[[str], Dict[str, List[Dict[str, Any]]]]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Exécute l'extraction d'attributs de fichiers en parallèle et accumule les résultats."""
        data: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        files = self.get_source_files()
        total = len(files)

        if not files:
            return data

        def worker(idx: int, file_path: str):
            self.logger.log_file_progress(idx, total, file_path)
            return extract_func(file_path)

        # Max_workers peut être ajusté selon tes besoins (ex: max_workers=8)
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [
                executor.submit(worker, idx, file_path)
                for idx, file_path in enumerate(files, start=1)
            ]

            for future in as_completed(futures):
                attributes = future.result()
                for codec_type, dicos in attributes.items():
                    data[codec_type].extend(dicos)

        return data
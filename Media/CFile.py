from pathlib import Path

from Common.EEnums import EActionType, EMediaType
from Common.Helpers.File import get_file_common_fields
from Media.CMedia import CMedia, FileAttributes


class CFile(CMedia):
    def __init__(
        self,
        json_path: str | Path,
        action_type: EActionType
        )-> None:
        super().__init__(json_path, EMediaType.File, action_type)

    def read(self)-> None:
        """Lit les fichiers génériques et exporte l'analyse sous forme de fichier CSV."""
        if not self.other_source_path:
            self.logger.log("Warning: other_source_path non défini pour le stockage des CSV.")
            return

        data = self.process_files_parallel(self._get_file_attributes_fields)
        self.export_dataframes_to_csv_by_codec(self.build_dataframes_by_codec(data), is_other_source=True)

    def _get_file_attributes_fields(
        self,
        file_path: str | Path
    )-> FileAttributes:
        """Récupère les métadonnées de base du fichier et le déplace/copie si configuré."""
        path_obj = Path(file_path)
        self.logger.log(f"Get attributes fields of {path_obj}")

        try:
            if self.is_move_or_copy:
                path_obj = self._move_or_copy_by_extension(path_obj)
            return {"file": [get_file_common_fields(path_obj, True)]}

        except Exception as e:  # noqa: BLE001
            self.logger.log_error(f"Error with {path_obj} : failed to get fields ({e})")
            return {}

    def _move_or_copy_by_extension(self, path_obj: Path)-> Path:
        """Range le fichier dans <cible>/<extension>/ ; un doublon (même taille, date et hash)
        n'est pas recopié, un homonyme différent reçoit un suffixe numérique."""
        if not self.target_path:
            raise ValueError("target_path non défini pour le déplacement/copie")

        file_info = get_file_common_fields(path_obj, True)
        extension = path_obj.suffix.lower()
        target_dir = self.target_path / extension.lstrip(".")

        def file_name_func(idx: int, _: str)-> str:
            return str(target_dir / f"{path_obj.stem}{idx or ''}{extension}")

        def is_same_media_func(other_file: str | Path)-> bool:
            other_path = Path(other_file)
            return other_path.is_file() and file_info == get_file_common_fields(other_path, True)

        return self._move_or_copy(path_obj, target_dir, file_name_func, is_same_media_func)

    def write(self)-> None:
        """Met à jour les tags des fichiers audio/vidéo à partir de la source CSV."""
        self._write_tags_from_csv("file")

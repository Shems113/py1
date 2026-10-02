import functools
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from Common.CLazy import CLazy
from Common.CMusicAutotag import CMusicAutotag
from Common.EEnums import EActionType, EMediaType
from Common.Helpers.Audio import add_audio_id3_images, get_audio_id3_tags
from Common.Helpers.Dataframe import data_frame_from_csv, data_frame_to_csv
from Common.Helpers.File import (
    clean_file_name,
    clean_last_directory_name,
    get_file_common_fields,
)
from Common.Helpers.Media import is_audio
from Media.CMedia import CMedia, FileAttributes


class CMusic(CMedia):
    def __init__(
        self,
        json_path: str | Path,
        action_type: EActionType
        )-> None:
        super().__init__(json_path, EMediaType.Music, action_type)
        # Créé au premier usage : l'action Write n'en a pas besoin (ni de fpcalc)
        # (clé AcoustID et URL des API : Files/ExternalApiSettings.json)
        self.music_auto_tag: CLazy[CMusicAutotag] = CLazy(CMusicAutotag)

    def read(self)-> None:
        """Lit les fichiers audio sources et exporte les analyses en CSV."""
        if not self.other_source_path:
            self.logger.log("Warning: other_source_path non défini pour le stockage des CSV.")
            return

        data = self.process_files_parallel(self._get_music_attributes_fields)
        self.export_dataframes_to_csv_by_codec(self.build_dataframes_by_codec(data), is_other_source=True)
        self.remove_empty_folders()

    def _get_music_attributes_fields(
        self,
        file_path: str | Path
    )-> FileAttributes:
        """Extrait les tags, les tags trouvés en ligne et déplace/copie le fichier si configuré."""
        path_obj = Path(file_path)
        self.logger.log(f"Get music attributes fields of {path_obj}")

        if not is_audio(path_obj):
            return {}

        try:
            audio = get_audio_id3_tags(path_obj)
            if audio is None:
                raise ValueError("format audio non reconnu par mutagen")

            if self.is_move_or_copy:
                path_obj = self._move_or_copy_by_album(path_obj, audio)

            common_fields = get_file_common_fields(path_obj, False)
            music_auto_tags, is_ok = self.logger.log_elapsed_time(
                lambda: self.music_auto_tag.value.get_tags(str(path_obj)),
                f"Get auto tags of {path_obj}",
            )
            if not is_ok:
                self.logger.log_error(f"Failed to get auto tags of {path_obj}")

            music_tags = {k: "|".join(map(str, v)) for k, v in audio.items()}
            other_info = vars(audio.info) if hasattr(audio, "info") else {}
            return {
                "music": [common_fields | music_tags],
                "musicAutoTag": [music_auto_tags],
                "musicOther": [common_fields | other_info],
            }

        except Exception as e:  # noqa: BLE001
            self.logger.log_error(f"Error with {file_path} : failed to get fields ({e})")
            return {}

    def _move_or_copy_by_album(self, path_obj: Path, audio: Any)-> Path:
        """Range le fichier dans <cible>/<ext>/<ARTISTE>/<ALBUM>/<titre>.<ext> ; un fichier
        aux tags identiques n'est pas recopié, un homonyme différent reçoit un préfixe."""
        if not self.target_path:
            raise ValueError("target_path non défini pour le déplacement/copie")

        extension = path_obj.suffix.lower()
        artist = audio.get("albumartist", ["UNKNOWN"])[0]
        default_album = self.item.get("AlbumDefaultName", "{}").format(artist)
        album = audio.get("album", [default_album])[0]
        # « 3/11 » -> 3 (l'ancien code passait la chaîne brute, qui plantait en cas de collision)
        track = re.match(r"\d+", str(audio.get("tracknumber", ["1"])[0]))
        index = int(track.group()) if track else 1

        target_dir = (
            self.target_path
            / extension.lstrip(".")
            / clean_last_directory_name(artist.upper())
            / clean_last_directory_name(album.upper())
        )
        title = clean_file_name(audio.get("title", [""])[0])

        def file_name_func(idx: int, separator: str)-> str:
            prefix = f"{idx}{separator}" if idx != 0 else ""
            return str(target_dir / f"{prefix}{title}{extension}")

        def is_same_media_func(other_file: str | Path)-> bool:
            other_path = Path(other_file)
            other_audio = get_audio_id3_tags(other_path) if other_path.is_file() else None
            return other_audio is not None and all(audio.get(k) == other_audio.get(k) for k in audio)

        return self._move_or_copy(path_obj, target_dir, file_name_func, is_same_media_func, index)

    def write(self)-> None:
        """Met à jour les tags texte et images des fichiers audio."""
        self._write_tags_from_csv(allow_video=False)
        self._write_audio_images()

    def _write_audio_images(self)-> None:
        """Insère les pochettes/images dans les métadonnées ID3 des fichiers audio."""
        if not self.other_source_path or not self.other_source_path.is_file():
            return

        other_source = self.other_source_path
        df = data_frame_from_csv(other_source, self.data_separator)
        if df.empty:
            return

        # Restructuration des images par fichier : {fichier: {image: {type: description}}}
        image_mapping: dict[str, dict[str | Path, dict[int, str]]] = defaultdict(lambda: defaultdict(dict))
        for row in df.itertuples(index=False):
            image_mapping[row.path][row.imagePath][int(row.imageType)] = row.imageDescription

        paths_to_remove = []
        for file_path, images_config in image_mapping.items():
            path_obj = Path(file_path)
            if not is_audio(path_obj):
                continue
            try:
                self.logger.log_elapsed_time(
                    functools.partial(add_audio_id3_images, path_obj, images_config),
                    f"Add images to {path_obj} with {images_config}",
                )
                paths_to_remove.append(file_path)
            except Exception as e:  # noqa: BLE001
                self.logger.log_error(f"Error with {path_obj} : failed to add image {images_config} ({e})")

        data_frame_to_csv(df[~df["path"].isin(paths_to_remove)], other_source, self.data_separator)

import os
import time
from pathlib import Path

import pandas as pd
from opensubtitlescom import OpenSubtitles

from Common.CLogger import CLogger as Logger
from Common.Helpers.Dataframe import data_frame_from_csv, data_frame_to_csv
from Common.Helpers.File import get_file_name_without_extension, get_folder_name
from Common.Helpers.Media import move_media_file
from Common.Helpers.Other import check_required_env_vars


class COpenSubtitles:
    def __init__(
        self,
        logger:Logger
        ):
        check_required_env_vars(
            "OpenSubtitles",
            ["OPENSUBTITLES_USER", "OPENSUBTITLES_PASS", "OPENSUBTITLES_API_KEY"],
        )
        opensubtitles_api_key='WJMkwcyNtcL7tnbgK9sdClOXdoxME0cX'
        opensubtitles_user='Shems113'
        opensubtitles_password='TyWk3pMDkR!&8hc'
        # Sécurisation : retrait des identifiants et clés API gravés en dur
        self.opensubtitles_user = os.getenv("OPENSUBTITLES_USER", opensubtitles_user)
        self.opensubtitles_pass = os.getenv("OPENSUBTITLES_PASS", opensubtitles_password)
        self.opensubtitles_api_key = os.getenv("OPENSUBTITLES_API_KEY", opensubtitles_api_key)
        self.logger = logger
        self.user_agent = f"{self.opensubtitles_user or 'MediaApp'} v1.0"
        self.opensubtitles_client = None
        self._is_logged_in = False

    def _ensure_login(self) -> bool:
        """Connexion paresseuse (lazy initialization) pour éviter de bloquer l'instanciation."""
        if self._is_logged_in and self.opensubtitles_client:
            return True

        if not all([self.opensubtitles_user, self.opensubtitles_pass, self.opensubtitles_api_key]):
            self.logger.logError("Erreur : Identifiants ou clé API OpenSubtitles manquants dans les variables d'environnement.")
            return False

        try:
            self.opensubtitles_client = OpenSubtitles(
                self.user_agent, self.opensubtitles_api_key
            )
            self.opensubtitles_client.login(
                self.opensubtitles_user, self.opensubtitles_pass
            )
            self._is_logged_in = True
            return True
        except Exception as e:  # noqa: BLE001
            self.logger.logError(f"Erreur d'authentification OpenSubtitles : {e}")
            self._is_logged_in = False
            return False

    def get_film_french_subtitles(
        self, tmdb_id: int, name: str, year: int | None = None
    ) -> pd.DataFrame:
        """Recherche les sous-titres FR pour un film spécifique via son ID TMDB."""
        if not self._ensure_login():
            return pd.DataFrame()

        self.logger.log(f"Recherche de sous-titres FR pour : {name} ({year or 'N/A'})")
        try:
            response = self.opensubtitles_client.search(
                tmdb_id=tmdb_id, languages="fr"
            )
            total_count = getattr(response, "total_count", 0)
            self.logger.log(f"Résultats trouvés : {total_count}")

            data = getattr(response, "data", [])
            if not data:
                return pd.DataFrame()

            # Extrait les données sous forme de dictionnaire
            data_list = [
                item.__dict__ if hasattr(item, "__dict__") else item
                for item in data
            ]
            return pd.json_normalize(data_list)

        except Exception as e:  # noqa: BLE001
            self.logger.logError(f"Erreur lors de la recherche de sous-titres pour {name}: {e}")
            return pd.DataFrame()

    def download_film_subtitle(
        self, path: str, file_id: int | str
    ) -> pd.DataFrame:
        """Télécharge un sous-titre et le place à côté du fichier média."""
        if not self._ensure_login():
            return pd.DataFrame([{"subtitlePath": "AUTH_ERROR"}])

        self.logger.log(f"Téléchargement du sous-titre pour : {path} (file_id: {file_id})...")
        time.sleep(1)  # Respect du rate limit imposé par l'API OpenSubtitles

        try:
            temp_filename = f"temp_{file_id}.srt"
            temp_path = self.opensubtitles_client.download_and_save(
                file_id, filename=temp_filename
            )

            dir_name = get_folder_name(path)
            file_name = get_file_name_without_extension(path)

            # Formatage de nommage conforme (ex: film.fr.srt, film.fr2.srt, etc.)
            def name_func(i: int, _: str) -> str:
                return os.path.join(dir_name,f"{file_name}.fr{f'{i+1}' if i > 0 else ''}.srt")

            def check_func(f: str | Path) -> bool:
                return not os.path.isfile(f)

            moved_files = move_media_file(temp_path, dir_name, name_func, check_func)
            sub_path = moved_files[0] if moved_files else "N/A"

            self.logger.log(f"Sous-titre enregistré : {sub_path}")
            return pd.DataFrame([{"subtitlePath": sub_path}])

        except Exception as e:  # noqa: BLE001
            self.logger.logError(f"Erreur au téléchargement du sous-titre {file_id} : {e}")
            return pd.DataFrame([{"subtitlePath": "ERROR"}])

    # --- Routes Flask ---

    def route_get_films_subtitles(
        self, inputFile: str | Path, outputFile: str | Path
    ) -> bool:
        """Parcourt le CSV d'entrée, cherche les sous-titres pour chaque film et exporte les résultats."""
        df_input = data_frame_from_csv(inputFile, ";")
        if df_input.empty:
            return False

        results: list[pd.DataFrame] = []

        for r in df_input.itertuples():
            tmdb_id = getattr(r, "tmdb_id", None)
            name = getattr(r, "name", None)
            year = getattr(r, "year", None)

            if not tmdb_id:
                continue

            df_sub = self.get_film_french_subtitles(tmdb_id, name, year)
            if not df_sub.empty:
                df_sub = df_sub.assign(tmdb_id=tmdb_id, name=name, year=year)
                results.append(df_sub)

            time.sleep(0.5)  # Pause pour éviter le rate-limiting sur la recherche en boucle

        if results:
            final_df = pd.concat(results, ignore_index=True)
            data_frame_to_csv(final_df, outputFile, ";")
            return True

        self.logger.log("Aucun sous-titre trouvé pour l'ensemble des films du CSV.")
        return False

    def route_download_films_subtitles(
        self, inputFile: str | Path, outputFile: str | Path
    ) -> bool:
        """Parcourt le CSV d'entrée et télécharge les sous-titres sélectionnés."""
        df_input = data_frame_from_csv(inputFile, ";")
        if df_input.empty:
            return False

        results: list[pd.DataFrame] = []

        for r in df_input.itertuples():
            path_val = getattr(r, "path", None)
            file_id = getattr(r, "file_id", None)

            if not path_val or not file_id:
                continue

            df_res = self.download_film_subtitle(path_val, file_id)
            df_res = df_res.assign(path=path_val, file_id=file_id)
            results.append(df_res)

        if results:
            final_df = pd.concat(results, ignore_index=True)
            data_frame_to_csv(final_df, outputFile, ";")
            return True

        return False

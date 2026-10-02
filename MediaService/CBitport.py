import urllib.parse
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from Common.CLogger import CLogger as Logger
from Common.Helpers.ApiSettings import get_api_settings, update_api_settings
from Common.Helpers.Dataframe import data_frame_from_csv, data_frame_to_csv
from Common.Helpers.Torrent import (
    select_best_quality_torrent,
    select_torrents_to_download,
)
from Common.Helpers.Web import handle_response

REQUEST_TIMEOUT = 30  # secondes : sans limite, une API muette bloquerait la route Flask


class CBitport:
    def __init__(
        self,
        logger: Logger
        )-> None:
        # URL et identifiants : section « Bitport » de Files/ExternalApiSettings.json.
        # AccessToken peut être vide : il est obtenu via le flux OAuth (/callback).
        settings = get_api_settings("Bitport")
        self.logger = logger
        self.bitport_base_url = settings["BaseUrl"]
        self.bitport_client_id = str(settings["ClientId"])
        self.bitport_client_secret = settings["ClientSecret"]
        self.bitport_access_token = settings.get("AccessToken", "")
        self.bitport_redirect_url = settings["RedirectUrl"]

        redirect_uri_encoded = urllib.parse.quote(self.bitport_redirect_url, safe="")
        self.bitport_auth_url = (
            f"{self.bitport_base_url}/oauth2/authorize"
            f"?response_type=code&client_id={self.bitport_client_id}&redirect_uri={redirect_uri_encoded}"
        )

        self.bitport_session = requests.Session()
        self.bitport_session.headers.update({"accept": "application/json"})
        self._set_access_token(self.bitport_access_token)

    def _set_access_token(self, access_token: str | None, save: bool = False)-> None:
        if access_token:
            self.bitport_access_token = access_token
            self.bitport_session.headers["Authorization"] = f"Bearer {access_token}"
            if save:  # conservé pour les prochains lancements
                update_api_settings("Bitport", {"AccessToken": access_token})

    def _get(self, path: str, **kwargs: Any)-> dict[str, Any]:
        res = self.bitport_session.get(f"{self.bitport_base_url}{path}", timeout=REQUEST_TIMEOUT, **kwargs)
        return handle_response(res) or {}

    def _post(self, path: str, data: dict[str, Any], include_json: bool = False)-> requests.Response:
        res = self.bitport_session.post(f"{self.bitport_base_url}{path}", data=data, timeout=REQUEST_TIMEOUT)
        handle_response(res, include_json=include_json)
        return res

    # --- Authentification & OAuth2 ---

    def callback_bitport_access_token(
        self,
        code: str
        )-> dict[str, Any]:
        """Échange le code reçu lors de la redirection OAuth contre un Access Token et un Refresh Token."""
        json_data = handle_response(requests.post(
            f"{self.bitport_base_url}/oauth2/access-token",
            data={
                "client_id": self.bitport_client_id,
                "client_secret": self.bitport_client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.bitport_redirect_url,
            },
            timeout=REQUEST_TIMEOUT,
        )) or {}
        self._set_access_token(json_data.get("access_token"), save=True)
        return json_data

    def refresh_access_token(
        self,
        refresh_token: str
        )-> dict[str, Any]:
        """Renouvelle le jeton d'accès sans re-passer par le navigateur."""
        json_data = handle_response(requests.post(
            f"{self.bitport_base_url}/oauth2/access-token",
            data={
                "client_id": self.bitport_client_id,
                "client_secret": self.bitport_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=REQUEST_TIMEOUT,
        )) or {}

        tokens = json_data.get("data", {})
        if tokens.get("access_token"):
            self._set_access_token(tokens["access_token"], save=True)
            self.logger.log("Nouveau jeton d'accès Bitport mis à jour avec succès.")
        return tokens

    # --- Endpoints Cloud & Action ---

    def discover_bitport_cloud_folders(self)-> dict[str, Any]:
        """Récupère l'arborescence complète des dossiers sur Bitport."""
        return self._get("/cloud/byPath", params={"scope": "recursive"})

    def _root_folder(self)-> dict[str, Any]:
        """Dossier « root » du cloud Bitport (avec ses sous-dossiers)."""
        folders = self.discover_bitport_cloud_folders().get("data", [])
        return next((f for f in folders if f.get("name") == "root"), {})

    def discover_bitport_cloud_videos(
        self,
        output_file: str | Path | None = None
    )-> dict[str, Any]:
        """Récupère la liste de toutes les vidéos sur le cloud et l'exporte éventuellement en CSV."""
        res = self._get("/cloud/all-videos")
        if res and output_file:
            data_frame_to_csv(pd.json_normalize(res, record_path=["data", "files"]), output_file, ";")
        return res

    def create_film_folders_in_bitport(
        self,
        input_file: str | Path
        )-> bool:
        """Crée les dossiers manquants sur Bitport à partir d'un fichier CSV d'entrée."""
        root_data = self._root_folder()
        root_code = root_data.get("code")
        if not root_code:
            self.logger.log_error("Erreur: Impossible de trouver le dossier 'root' sur Bitport.")
            return False

        existing_folders = {f.get("name") for f in root_data.get("folders", []) if "name" in f}

        df_input = data_frame_from_csv(input_file, ";")
        for col in ["tmdb_id", "name"]:
            if col not in df_input.columns:
                self.logger.log_error(f"Erreur: Le fichier d'entrée doit contenir une colonne '{col}'.")
                return False

        folder_names = (df_input["name"].str.strip() + "_" + df_input["tmdb_id"].astype("string")).dropna().unique()

        for folder_name in folder_names:
            if not folder_name or folder_name in existing_folders:
                continue
            self.logger.log(f"Création du dossier Bitport : {folder_name}")
            if self._post(f"/cloud/{root_code}", {"name": folder_name}).status_code in (200, 201):
                existing_folders.add(folder_name)

        return True

    def transfer_torrents_to_bitport(
        self,
        input_file: str | Path,
        output_file: str | Path
    )-> bool:
        """Sélectionne les meilleurs torrents et lance leurs transferts vers les dossiers Bitport correspondants."""
        df_best = select_best_quality_torrent(data_frame_from_csv(input_file, ";"))
        if df_best.empty:
            self.logger.log("Aucun torrent sélectionné pour le transfert.")
            return False

        data_frame_to_csv(df_best, output_file, ";")

        folder_map = {
            f["name"]: f["code"] for f in self._root_folder().get("folders", []) if "name" in f and "code" in f
        }

        for row in select_torrents_to_download(df_best).itertuples():
            tmdb_id, name = row.tmdb_id, str(row.name).strip()
            folder_name = f"{name}_{tmdb_id}"
            folder_code = folder_map.get(folder_name)

            if not folder_code:  # dossier renommé : on le retrouve par l'identifiant TMDB
                folder_name, folder_code = next(
                    ((k, v) for k, v in folder_map.items() if k.split("_")[-1] == str(tmdb_id)), (folder_name, None))

            if not folder_code:
                self.logger.log(f"Attention: Dossier '{folder_name}' non trouvé sur Bitport pour le film '{name}'.")
                continue

            self.logger.log(f"Transfert de '{name}' vers le dossier Bitport '{folder_name}'")
            self._post("/transfers", {"folder_code": folder_code, "torrent": row.magnet}, include_json=True)

        return True

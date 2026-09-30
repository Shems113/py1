import os
import urllib.parse
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from Common.CLogger import CLogger as Logger
from Common.Helpers.Dataframe import data_frame_from_csv, data_frame_to_csv
from Common.Helpers.System import check_required_env_vars
from Common.Helpers.Torrent import select_best_quality_torrent
from Common.Helpers.Web import handle_response


class CBitport:
    def __init__( 
        self,
        logger:Logger
        )-> None:
        # Avertissement explicite et immédiat si les credentials sont absents, plutôt
        # que de laisser échouer silencieusement le premier appel API (401/403 opaque).
        # BITPORT_ACCESS_TOKEN n'est volontairement pas listé ici : il peut être obtenu
        # dynamiquement via le flux OAuth (/callback) sans être défini au démarrage.
        check_required_env_vars("Bitport", ["BITPORT_CLIENT_ID", "BITPORT_CLIENT_SECRET"])
        bitport_client_id=155811867
        bitport_client_secret='chpff96gnt9nbf74gk'
        bitport_access_token='7d0982d8f7c936136b863572c14be3f3ce2a306c'
        self.logger = logger
        # Utilisation de variables d'environnement sans valeurs d'API sensibles en clair
        self.bitport_client_id = os.getenv("BITPORT_CLIENT_ID", str(bitport_client_id))
        self.bitport_client_secret = os.getenv("BITPORT_CLIENT_SECRET", bitport_client_secret)
        self.bitport_access_token = os.getenv("BITPORT_ACCESS_TOKEN", bitport_access_token)
        self.bitport_redirect_url = "http://localhost:5000/callback"

        redirect_uri_encoded = urllib.parse.quote(self.bitport_redirect_url, safe="")
        self.bitport_auth_url = (
            f"https://api.bitport.io/v2/oauth2/authorize"
            f"?response_type=code&client_id={self.bitport_client_id}&redirect_uri={redirect_uri_encoded}"
        )
        self.bitport_base_url = "https://api.bitport.io/v2"

        self.bitport_session = requests.Session()
        self.bitport_session.headers.update(
            {
                "accept": "application/json",
                "Authorization": f"Bearer {self.bitport_access_token}",
            }
        )

    # --- Authentification & OAuth2 ---

    def callback_bitport_access_token(
        self,
        code: str
        )-> dict[str, Any]:
        """Échange le code reçu lors de la redirection OAuth contre un Access Token et un Refresh Token."""
        url = f"{self.bitport_base_url}/oauth2/access-token"
        payload = {
            "client_id": self.bitport_client_id,
            "client_secret": self.bitport_client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.bitport_redirect_url,
        }

        res = requests.post(url, data=payload ,timeout=30)
        json_data = handle_response(res) or {}
        access_token = json_data.get("access_token")

        if access_token:
            self.bitport_access_token = access_token
            self.bitport_session.headers.update(
                {"Authorization": f"Bearer {access_token}"}
            )

        return json_data

    def refresh_access_token(
        self,
        refresh_token: str
        )-> dict[str, Any]:
        """Renouvelle le jeton d'accès sans re-passer par le navigateur."""
        url = f"{self.bitport_base_url}/oauth2/access-token"
        payload = {
            "client_id": self.bitport_client_id,
            "client_secret": self.bitport_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }

        res = requests.post(url, data=payload , timeout=30)
        json_data = handle_response(res) or {}

        tokens = json_data.get("data", {})
        new_access_token = tokens.get("access_token")

        if new_access_token:
            self.bitport_access_token = new_access_token
            self.bitport_session.headers.update(
                {"Authorization": f"Bearer {new_access_token}"}
            )
            self.logger.log("Nouveau jeton d'accès Bitport mis à jour avec succès.")

        return tokens

    # --- Endpoints Cloud & Action ---

    def discover_bitport_cloud_folders(self)-> dict[str, Any]:
        """Récupère l'arborescence complète des dossiers sur Bitport."""
        url = f"{self.bitport_base_url}/cloud/byPath"
        res = self.bitport_session.get(url, params={"scope": "recursive"})
        return handle_response(res) or {}

    def discover_bitport_cloud_videos(
        self,
        outputFile: str | Path | None = None
    )-> dict[str, Any]:
        """Récupère la liste de toutes les vidéos sur le cloud et l'exporte éventuellement en CSV."""
        url = f"{self.bitport_base_url}/cloud/all-videos"
        res = handle_response(self.bitport_session.get(url)) or {}

        # files_data = (
        #     res.get("data", {"files": []}).get("files", []) if isinstance(res, dict) else []
        # )

        # L'export CSV ne s'exécute que si un chemin de sortie est fourni
        if res and outputFile:
            df_files = pd.json_normalize(res,record_path=['data','files'])
            data_frame_to_csv(df_files, outputFile, ";")

        return res

    def create_film_folders_in_bitport(
        self,
        inputFile: str | Path
        )-> bool:
        """Crée les dossiers manquants sur Bitport à partir d'un fichier CSV d'entrée."""
        cloud_folders = self.discover_bitport_cloud_folders()
        root_data = next(
            (f for f in cloud_folders.get("data", []) if f.get("name") == "root"), {}
        )
        root_code = root_data.get("code")

        if not root_code:
            self.logger.log_error("Erreur: Impossible de trouver le dossier 'root' sur Bitport.")
            return False

        existing_folders = {
            f.get("name") for f in root_data.get("folders", []) if "name" in f
        }

        df_input = data_frame_from_csv(inputFile, ";")
        for col in ["tmdb_id", "name"]:
            if col not in df_input.columns:
                self.logger.log_error(f"Erreur: Le fichier d'entrée doit contenir une colonne '{col}'.")
                return False

        unique_folder_names = (
            (df_input["name"].str.strip() + "_" + df_input["tmdb_id"].astype('string')).dropna().unique()
        )

        for folder_name in unique_folder_names:
            if not folder_name or folder_name in existing_folders:
                continue

            self.logger.log(f"Création du dossier Bitport : {folder_name}")
            url = f"{self.bitport_base_url}/cloud/{root_code}"
            res = self.bitport_session.post(url, data={"name": folder_name})
            if res.status_code in (200, 201):
                existing_folders.add(folder_name)

        return True

    def transfer_torrents_to_bitport(
        self,
        inputFile: str | Path,
        outputFile: str | Path
    )-> bool:
        """Sélectionne les meilleurs torrents et lance leurs transferts vers les dossiers Bitport correspondants."""
        df_input = data_frame_from_csv(inputFile, ";")
        df_best = select_best_quality_torrent(df_input)

        if df_best.empty:
            self.logger.log("Aucun torrent sélectionné pour le transfert.")
            return False

        data_frame_to_csv(df_best, outputFile, ";")

        cloud_folders = self.discover_bitport_cloud_folders()
        root_folders = next(
            (
                f.get("folders", [])
                for f in cloud_folders.get("data", [])
                if f.get("name") == "root"
            ),
            [],
        )

        folder_map = {
            f["name"]: f["code"] for f in root_folders if "name" in f and "code" in f
        }

        for row in df_best[df_best["selection_status"].isin(["#1_best", "#2_best_quality_lower_size"])].itertuples():
            tmdb_id = getattr(row, "tmdb_id", None)
            name = getattr(row, "name", None)
            magnet_val = getattr(row, "magnet", None)

            if not magnet_val or pd.isna(magnet_val):
                continue

            folder_name = (name.strip() + "_" + str(tmdb_id))
            folder_code = folder_map.get(folder_name)

            if not folder_code:
                (folder_name,folder_code) = next(((k,v) for k,v in folder_map.items() if (k.split("_")[-1]==str(tmdb_id))), (None, None))

            if not folder_code:
                self.logger.log(
                    f"Attention: Dossier '{folder_name}' non trouvé sur Bitport pour le film '{name}'."
                )
                continue

            self.logger.log(f"Transfert de '{name}' vers le dossier Bitport '{folder_name}'")
            payload = {"folder_code": folder_code, "torrent": magnet_val}

            res = self.bitport_session.post(
                f"{self.bitport_base_url}/transfers", data=payload
            )
            handle_response(res, include_json=True)

        return True

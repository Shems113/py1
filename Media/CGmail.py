import base64
import json
import typing
from collections import defaultdict
from email.utils import parseaddr
from pathlib import Path

import pandas as pd
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from Common.EEnums import EActionType, EMediaType
from Common.Helpers.Dataframe import data_frame_from_csv, data_frame_to_csv
from Common.Helpers.File import clean_file_name, get_file_name_extension
from Common.Helpers.Json import (
    json_from_file,
    json_to_file,
)
from Media.CMedia import CMedia


class CGmail(CMedia):
    USER_ID = "me"
    LIST_MESSAGE_MAX_RESULTS = 500
    SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]  # noqa: RUF012
    MESSAGE_HEADERS = [  # noqa: RUF012
        "Date",
        "From",
        "To",
        "Subject",
        "Delivered-To",
        "Content-Type",
        "Return-Path",
        "In-Reply-To",
    ]

    def __init__(
        self,
        json_path: str | Path,
        action_type: EActionType
    ) -> None:
        super().__init__(json_path, EMediaType.Gmail, action_type)
        self.gmail_api_service: Resource | None = self._call_gmail_api_service()

    def _call_gmail_api_service(self) -> Resource | None:
        """Authentifie l'utilisateur et initialise le service de l'API Gmail."""
        if not self.source_path or not self.source_path.is_file():
            self.logger.logError(
                "Error: source_path invalide pour la configuration Gmail API."
            )
            return None

        json_gmail_settings = json_from_file(self.source_path)

        credentials = None
        if "token" in json_gmail_settings:
            credentials = Credentials.from_authorized_user_info(
                json_gmail_settings["token"], self.SCOPES
            )

        # Si les identifiants ne sont pas valides, relancer la procédure d'authentification
        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_config(
                    json_gmail_settings["credentials"], self.SCOPES
                )
                credentials = flow.run_local_server(port=0)

            # Sauvegarde des identifiants mis à jour
            json_gmail_settings["token"] = json.loads(credentials.to_json())
            json_to_file(self.source_path, json_gmail_settings)

        try:
            self.logger.log("Call the Gmail API")
            return build("gmail", "v1", credentials=credentials)
        except HttpError as error:
            self.logger.logError(
                f"An error occurred while connecting to Gmail API: {error}"
            )
            return None

    def read(self) -> None:
        """Lit les informations de base Gmail (labels, messages) et les exporte sous forme de CSV."""
        if not self.target_path:
            self.logger.log(
                "Warning: target_path non défini pour le stockage des CSV."
            )
            return

        dfs = self._build_dataframes()
        self.export_dataframes_to_csv_by_codec(dfs)

    def _build_dataframes(self) -> dict[str, pd.DataFrame]:
        """Construit les DataFrames résumant les labels et la liste des messages."""
        results = {}
        for data_type, func in [("gmailLabel",self._list_labels), ("gmailMessage", self._list_messages)]:
            results[data_type] = pd.DataFrame()
            try:
                results[data_type] = self.logger.logElapsedTime(
                    func, f"Build dataframe with {data_type[5:]}s"
                )
            except HttpError as error:
                self.logger.logError(f"An error occurred getting {data_type[5:]}s: {error}")
                results[data_type] = pd.DataFrame()

        return results

    def _list_labels(self) -> pd.DataFrame:
        """Récupère la liste des étiquettes (labels) de l'utilisateur."""
        if not self.gmail_api_service:
            return pd.DataFrame()

        request = self.gmail_api_service.users().labels().list(
            userId=self.USER_ID
        )
        response = self.logger.logElapsedTime(
            request.execute, "Get Gmail labels list"
        )
        return pd.DataFrame(response.get("labels", []))

    def _list_messages(
        self,
        label_ids: list[str] | None = None
    ) -> pd.DataFrame:
        """Récupère la liste complète des messages correspondant au motif avec pagination."""
        if not self.gmail_api_service:
            return pd.DataFrame()

        label_ids = label_ids or []
        query_pattern = self.file_patterns[0] if self.file_patterns else ""
        messages = []
        page_index = 0

        request = self.gmail_api_service.users().messages().list(
            userId=self.USER_ID,
            labelIds=label_ids,
            maxResults=self.LIST_MESSAGE_MAX_RESULTS,
            q=query_pattern,
        )

        while request is not None:
            response = self.logger.logElapsedTime(
                request.execute, f"Get Gmail messages list (#{page_index})"
            )
            messages.extend(response.get("messages", []))

            request = self.gmail_api_service.users().messages().list_next(
                previous_request=request, previous_response=response
            )
            page_index += 1

        return pd.DataFrame(messages)

    def write(self) -> None:
        """Récupère les détails avancés des messages (ainsi que leurs pièces jointes) et les exporte."""
        if not self.other_source_path or not self.other_source_path.is_file():
            self.logger.log(
                "Warning: other_source_path non présent pour la lecture des IDs de messages."
            )
            return

        target_dir = self.target_path if self.target_path else Path()
        df_source = data_frame_from_csv(
            self.other_source_path, self.data_separator
        )

        for key, df in self._build_dataframes_details(df_source).items():
            csv_path = target_dir / f"{key}.csv"

            def export_csv() -> None:
                data_frame_to_csv(df, csv_path, self.data_separator)  # noqa: B023

            self.logger.logElapsedTime(export_csv, f"Display {key}")

    def _build_dataframes_details(
        self,
        df_source: pd.DataFrame
    ) -> dict[str, pd.DataFrame]:
        """Extrait les détails et pièces jointes de chaque message identifié."""
        if df_source.empty or "id" not in df_source.columns:
            return {"gmailMessageDetailsResult": pd.DataFrame()}

        message_details = []
        unique_ids = set(df_source["id"].dropna())

        for message_id in unique_ids:
            try:
                message_details.append(self._get_message_details(message_id))
            except Exception as e:  # noqa: BLE001
                self.logger.logError(
                    f"An error occurred with message Id {message_id} : {e}"
                )

        def process_details_df() -> pd.DataFrame:
            df = pd.DataFrame(message_details)
            if df.empty:
                return df

            df = df.assign(
                header_Date=pd.to_datetime(
                    df["header_Date"], utc=True, errors="coerce"
                ).dt.strftime("%Y-%m-%d %H:%M:%S"),
                internalDate=pd.to_datetime(
                    pd.to_numeric(df["internalDate"]), unit="ms", errors="coerce"
                ).dt.strftime("%Y-%m-%d %H:%M:%S"),
            )
            return (
                df.explode("attachments")
                .reset_index(drop=True)
                .drop_duplicates(subset=["id", "attachments"])
            )

        df_result = self.logger.logElapsedTime(
            process_details_df, "Build dataframe with Gmail message details"
        )
        return {"gmailMessageDetailsResult": df_result}

    def _get_message_details(
        self,
        message_id: str
        ) -> dict[str, typing.Any]:
        """Récupère le détail complet d'un message donné via son identifiant."""
        if not self.gmail_api_service:
            return {}

        request = self.gmail_api_service.users().messages().get(
            userId=self.USER_ID, id=message_id, format="full"
        )
        response = self.logger.logElapsedTime(
            request.execute, f"Get message detail of Id {message_id}"
        )
        payload = response.get("payload", {})

        # Traitement des en-têtes HTTP/MIME
        raw_headers: dict[str, list[str]] = defaultdict(list)
        for header_elem in payload.get("headers", []):
            name = header_elem.get("name", "empty")
            value = header_elem.get("value", "")
            raw_headers[name].append(value)

        formatted_headers = {
            f"header_{header_name}": "|".join(
                raw_headers.get(header_name, [""])
            )
            for header_name in self.MESSAGE_HEADERS
        }

        # Extrait des pièces jointes si des sous-parties existent
        attachments = []
        parts = payload.get("parts", [])
        if parts:
            _, from_email = parseaddr(formatted_headers.get("header_From", ""))
            _, to_email = parseaddr(formatted_headers.get("header_To", ""))
            attachments = self._get_attachments(
                message_id, from_email, to_email, parts
            )

        response.pop("payload", None)
        return response | formatted_headers | {"attachments": attachments}

    def _get_attachments(
        self,
        message_id: str,
        from_email: str,
        to_email: str,
        parts: list[dict[str, typing.Any]],
    ) -> list[str]:
        """Télécharge et enregistre localement les pièces jointes associées à un message."""
        if not self.gmail_api_service:
            return []

        saved_file_paths = []

        def extract_parts(parts_list: list[dict[str, typing.Any]]) -> list[dict[str, typing.Any]]:
            """Extrait récursivement toutes les pièces jointes imbriquées."""
            extracted = []
            for part in parts_list:
                if part.get("body", {}).get("attachmentId"):
                    extracted.append(part)
                if "parts" in part:
                    extracted.extend(extract_parts(part["parts"]))
            return extracted

        attachment_parts = extract_parts(parts)

        for part in attachment_parts:
            filename = part.get("filename")
            body = part.get("body", {})
            attachment_id = body.get("attachmentId")

            if attachment_id:
                request = (
                    self.gmail_api_service.users()
                    .messages()
                    .attachments()
                    .get(
                        userId=self.USER_ID,
                        messageId=message_id,
                        id=attachment_id,
                    )
                )
                response = self.logger.logElapsedTime(
                    request.execute,
                    f"Get message attachment of Id {attachment_id}",
                )

                raw_data = response.get("data", "")
                file_data = base64.urlsafe_b64decode(raw_data.encode("UTF-8"))

                # Construction du nom de fichier et création de l'arborescence
                filename = (
                    filename if filename else f"attachment_{attachment_id}.txt"
                )
                ext = get_file_name_extension(filename)

                target_root = self.target_path if self.target_path else Path()
                folder = (
                    target_root
                    / "Gmail_Attachment"
                    / from_email
                    / to_email
                    / ext.replace(".", "")
                )
                folder.mkdir(parents=True, exist_ok=True)

                file_path = folder / clean_file_name(filename)
                file_path.write_bytes(file_data)

                abs_path_str = str(file_path.resolve())
                self.logger.log(
                    f"Attachment of message Id {message_id} saved : file://{abs_path_str}"
                )
                saved_file_paths.append(abs_path_str)

        return saved_file_paths

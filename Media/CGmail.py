import base64
import functools
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
from Common.Helpers.ApiSettings import get_api_settings, update_api_settings
from Common.Helpers.Dataframe import data_frame_from_csv, data_frame_to_csv
from Common.Helpers.File import (
    clean_file_name,
    clean_last_directory_name,
    get_file_name_extension,
)
from Media.CMedia import CMedia


class CGmail(CMedia):
    USER_ID = "me"
    LIST_MESSAGE_MAX_RESULTS = 500
    # Messages récupérés par requête groupée (batch) : Gmail accepte 100, mais au-delà
    # de ~50 il renvoie souvent des erreurs 429 (trop de requêtes simultanées)
    BATCH_SIZE = 50
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
        )-> None:
        super().__init__(json_path, EMediaType.Gmail, action_type)
        self.gmail_api_service: Resource | None = self._call_gmail_api_service()

    def _call_gmail_api_service(self)-> Resource | None:
        """Authentifie l'utilisateur et initialise le service de l'API Gmail."""
        # Identifiants OAuth, jeton et scopes : section « Gmail » de Files/ExternalApiSettings.json
        try:
            gmail_settings = get_api_settings("Gmail")
        except (FileNotFoundError, KeyError) as e:
            self.logger.log_error(f"Error: configuration Gmail API introuvable ({e})")
            return None

        scopes = gmail_settings["Scopes"]
        credentials = None
        if gmail_settings.get("Token"):
            credentials = Credentials.from_authorized_user_info(gmail_settings["Token"], scopes)

        # Si les identifiants ne sont pas valides, relancer la procédure d'authentification
        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_config(gmail_settings["Credentials"], scopes)
                credentials = flow.run_local_server(port=0)

            # Sauvegarde des identifiants mis à jour
            update_api_settings("Gmail", {"Token": json.loads(credentials.to_json())})

        try:
            self.logger.log("Call the Gmail API")
            return build("gmail", "v1", credentials=credentials)
        except HttpError as error:
            self.logger.log_error(
                f"An error occurred while connecting to Gmail API: {error}"
            )
            return None

    def read(self)-> None:
        """Lit les informations de base Gmail (labels, messages) et les exporte sous forme de CSV."""
        if not self.target_path:
            self.logger.log(
                "Warning: target_path non défini pour le stockage des CSV."
            )
            return

        dfs = self._build_dataframes()
        self.export_dataframes_to_csv_by_codec(dfs)

    def _build_dataframes(self)-> dict[str, pd.DataFrame]:
        """Construit les DataFrames résumant les labels et la liste des messages."""
        results = {}
        for data_type, label, func in [
            ("gmailLabel", "labels", self._list_labels),
            ("gmailMessage", "messages", self._list_messages),
        ]:
            try:
                results[data_type] = self.logger.log_elapsed_time(func, f"Build dataframe with {label}")
            except HttpError as error:
                self.logger.log_error(f"An error occurred getting {label}: {error}")
                results[data_type] = pd.DataFrame()
        return results

    def _list_labels(self)-> pd.DataFrame:
        """Récupère la liste des étiquettes (labels) de l'utilisateur."""
        if not self.gmail_api_service:
            return pd.DataFrame()

        request = self.gmail_api_service.users().labels().list(
            userId=self.USER_ID
        )
        response = self.logger.log_elapsed_time(
            request.execute, "Get Gmail labels list"
        )
        return pd.DataFrame(response.get("labels", []))

    def _list_messages(
        self,
        label_ids: list[str] | None = None
    )-> pd.DataFrame:
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
            response = self.logger.log_elapsed_time(
                request.execute, f"Get Gmail messages list (#{page_index})"
            )
            messages.extend(response.get("messages", []))

            request = self.gmail_api_service.users().messages().list_next(
                previous_request=request, previous_response=response
            )
            page_index += 1

        return pd.DataFrame(messages)

    def write(self)-> None:
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
            self.logger.log_elapsed_time(
                functools.partial(data_frame_to_csv, df, target_dir / f"{key}.csv", self.data_separator),
                f"Display {key}",
            )

    def _build_dataframes_details(
        self,
        df_source: pd.DataFrame
    )-> dict[str, pd.DataFrame]:
        """Extrait les détails et pièces jointes de chaque message identifié."""
        if df_source.empty or "id" not in df_source.columns:
            return {"gmailMessageDetailsResult": pd.DataFrame()}

        message_details = []
        unique_ids = sorted(set(df_source["id"].dropna()))
        responses = self.logger.log_elapsed_time(
            lambda: self._get_messages_batch(unique_ids),
            f"Get details of {len(unique_ids)} message(s) by batches of {self.BATCH_SIZE}",
        )

        for message_id in unique_ids:
            response = responses.get(message_id)
            if response is None:
                continue  # erreur déjà journalisée par _get_messages_batch
            try:
                message_details.append(self._get_message_details(message_id, response))
            except Exception as e:  # noqa: BLE001
                self.logger.log_error(
                    f"An error occurred with message Id {message_id} : {e}"
                )

        def process_details_df()-> pd.DataFrame:
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

        df_result = self.logger.log_elapsed_time(
            process_details_df, "Build dataframe with Gmail message details"
        )
        return {"gmailMessageDetailsResult": df_result}

    def _get_messages_batch(self, message_ids: list[str])-> dict[str, dict[str, typing.Any]]:
        """Détail complet des messages, par requêtes groupées (BATCH_SIZE messages par appel HTTP
        au lieu d'un appel par message). Un message en erreur est journalisé et absent du résultat."""
        if not self.gmail_api_service:
            return {}

        responses: dict[str, dict[str, typing.Any]] = {}

        def on_response(request_id: str, response: dict[str, typing.Any], exception: Exception | None)-> None:
            if exception is not None:
                self.logger.log_error(f"An error occurred with message Id {request_id} : {exception}")
            else:
                responses[request_id] = response

        messages = self.gmail_api_service.users().messages()
        for start in range(0, len(message_ids), self.BATCH_SIZE):
            batch = self.gmail_api_service.new_batch_http_request(callback=on_response)
            for message_id in message_ids[start:start + self.BATCH_SIZE]:
                batch.add(messages.get(userId=self.USER_ID, id=message_id, format="full"), request_id=message_id)
            batch.execute()
        return responses

    def _get_message_details(
        self,
        message_id: str,
        response: dict[str, typing.Any],
        )-> dict[str, typing.Any]:
        """En-têtes utiles et pièces jointes (téléchargées) d'un message déjà récupéré."""
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
    )-> list[str]:
        """Télécharge et enregistre localement les pièces jointes associées à un message."""
        if not self.gmail_api_service:
            return []

        saved_file_paths = []

        def extract_parts(parts_list: list[dict[str, typing.Any]])-> list[dict[str, typing.Any]]:
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
                response = self.logger.log_elapsed_time(
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
                    / (clean_last_directory_name(from_email) or "unknown")
                    / (clean_last_directory_name(to_email) or "unknown")
                    / (ext.lstrip(".") or "no_extension")
                )
                folder.mkdir(parents=True, exist_ok=True)

                file_path = self._free_attachment_path(folder / clean_file_name(filename), file_data)
                file_path.write_bytes(file_data)

                abs_path_str = str(file_path.resolve())
                self.logger.log(
                    f"Attachment of message Id {message_id} saved : file://{abs_path_str}"
                )
                saved_file_paths.append(abs_path_str)

        return saved_file_paths

    @staticmethod
    def _free_attachment_path(file_path: Path, data: bytes)-> Path:
        """Chemin où enregistrer la pièce jointe : le même si le fichier n'existe pas ou est
        identique, sinon nom_2, nom_3… (auparavant, deux pièces jointes homonymes de mails
        différents s'écrasaient)."""
        candidate, index = file_path, 1
        while candidate.exists() and candidate.read_bytes() != data:
            index += 1
            candidate = file_path.with_name(f"{file_path.stem}_{index}{file_path.suffix}")
        return candidate

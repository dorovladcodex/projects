from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import google.auth
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload


SCOPES = ["https://www.googleapis.com/auth/drive"]


def build_drive_service(client_file: Path, token_file: Path):
    credentials = None
    if token_file.exists():
        credentials = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid:
        if client_file.exists():
            flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
            credentials = flow.run_local_server(port=0)
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(credentials.to_json(), encoding="utf-8")
        else:
            credentials, _ = google.auth.default(scopes=SCOPES)
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def ensure_path(drive, names: list[str]) -> dict[str, str]:
    parent_id = None
    folder = None
    for name in names:
        folder = get_or_create_folder(drive, name, parent_id)
        parent_id = folder["id"]
    if folder is None:
        raise ValueError("Path must contain at least one folder name")
    return folder


def get_or_create_folder(drive, name: str, parent_id: str | None = None) -> dict[str, str]:
    parent_clause = f"'{parent_id}' in parents" if parent_id else "'root' in parents"
    query = (
        f"name = '{_escape_query(name)}' and "
        "mimeType = 'application/vnd.google-apps.folder' and "
        "trashed = false and "
        f"{parent_clause}"
    )
    result = drive.files().list(q=query, fields="files(id,name,webViewLink)", pageSize=1).execute()
    files = result.get("files", [])
    if files:
        return files[0]

    metadata: dict[str, Any] = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]
    return drive.files().create(body=metadata, fields="id,name,webViewLink").execute()


def download_json_file(drive, folder_id: str, name: str, fallback: Any) -> Any:
    file = find_file(drive, folder_id, name)
    if not file:
        return fallback
    request = drive.files().get_media(fileId=file["id"])
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    text = buffer.getvalue().decode("utf-8").strip()
    return json.loads(text) if text else fallback


def upload_file(drive, folder_id: str, file_path: Path, mime_type: str | None = None) -> str:
    existing = find_file(drive, folder_id, file_path.name)
    metadata = {"name": file_path.name}
    media = MediaFileUpload(str(file_path), mimetype=mime_type, resumable=False)
    if existing:
        updated = drive.files().update(
            fileId=existing["id"],
            media_body=media,
            body=metadata,
            fields="id,webViewLink",
        ).execute()
        return updated.get("webViewLink", f"https://drive.google.com/file/d/{updated['id']}/view")
    metadata["parents"] = [folder_id]
    created = drive.files().create(body=metadata, media_body=media, fields="id,webViewLink").execute()
    return created.get("webViewLink", f"https://drive.google.com/file/d/{created['id']}/view")


def find_file(drive, folder_id: str, name: str) -> dict[str, str] | None:
    query = f"name = '{_escape_query(name)}' and '{folder_id}' in parents and trashed = false"
    result = drive.files().list(q=query, fields="files(id,name,webViewLink)", pageSize=1).execute()
    files = result.get("files", [])
    return files[0] if files else None


def _escape_query(value: str) -> str:
    return value.replace("'", "\\'")

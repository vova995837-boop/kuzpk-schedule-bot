"""Read-only access to the bot's shared Google Drive data folder."""
import json
import os
import time
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials

DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_CACHE_TTL = 120
_cached_files = []
_cached_at = 0.0


def _session():
    if not DRIVE_FOLDER_ID or not SERVICE_ACCOUNT_JSON:
        return None
    info = json.loads(SERVICE_ACCOUNT_JSON)
    credentials = Credentials.from_service_account_info(info, scopes=[DRIVE_SCOPE])
    return AuthorizedSession(credentials)


def list_files(force_refresh=False):
    """List files in the shared folder and its subfolders, with relative paths."""
    global _cached_files, _cached_at
    if not DRIVE_FOLDER_ID or not SERVICE_ACCOUNT_JSON:
        return []
    if not force_refresh and _cached_files and time.monotonic() - _cached_at < _CACHE_TTL:
        return list(_cached_files)

    session = _session()
    pending = [(DRIVE_FOLDER_ID, "")]
    visited = set()
    files = []
    while pending:
        parent_id, parent_path = pending.pop(0)
        if parent_id in visited:
            continue
        visited.add(parent_id)
        page_token = None
        while True:
            params = {
                "q": f"'{parent_id}' in parents and trashed = false",
                "pageSize": 1000,
                "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime,parents)",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            response = session.get("https://www.googleapis.com/drive/v3/files", params=params, timeout=25)
            response.raise_for_status()
            payload = response.json()
            for item in payload.get("files", []):
                item["drive_path"] = "/".join(part for part in (parent_path, item["name"]) if part)
                if item.get("mimeType") == "application/vnd.google-apps.folder":
                    pending.append((item["id"], item["drive_path"]))
                else:
                    files.append(item)
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
    _cached_files = files
    _cached_at = time.monotonic()
    return list(files)


def download_file(file_id):
    """Download one Drive file's bytes by its Drive file ID."""
    if not DRIVE_FOLDER_ID or not SERVICE_ACCOUNT_JSON:
        raise RuntimeError("Google Drive is not configured")
    session = _session()
    response = session.get(
        f"https://www.googleapis.com/drive/v3/files/{quote(str(file_id), safe='')}?alt=media",
        timeout=45,
    )
    response.raise_for_status()
    return response.content


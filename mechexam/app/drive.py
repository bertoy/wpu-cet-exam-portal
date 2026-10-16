"""Google Drive uploads for performance-task submissions.

Credentials are read from the environment only -- ``GOOGLE_SERVICE_ACCOUNT_JSON``
(the JSON itself) or ``GOOGLE_SERVICE_ACCOUNT_FILE`` (a path).  No key is ever
stored in the repository.

One hard-won lesson is encoded here: a bare service account has **no Drive
storage quota of its own**, so uploading into a normal personal folder fails
with ``storageQuotaExceeded`` even when the folder is shared with it.  The two
working arrangements are:

1.  A **Shared drive** (Google Workspace), with the service account added as a
    member -- storage belongs to the drive, not the account.
2.  **Domain-wide delegation**, with ``GOOGLE_DRIVE_IMPERSONATE`` set to the
    instructor's address, so the upload is performed *as* the instructor and
    lands in their own Drive, owned by them.

``explain()`` reports which of these is in force, so a failure is diagnosable
from inside the app instead of by reading tracebacks.

Every submission is also stored locally first, so a Drive outage never costs a
student their work.
"""
from __future__ import annotations

import io
import json
import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]


@dataclass
class DriveResult:
    ok: bool
    file_id: str = ""
    url: str = ""
    error: str = ""
    hint: str = ""


def _library_available() -> bool:
    try:
        import google.oauth2.service_account  # noqa: F401
        import googleapiclient.discovery  # noqa: F401
    except Exception:
        return False
    return True


def _credentials_info() -> dict | None:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        raw = raw.strip()
        try:
            return json.loads(raw)
        except ValueError:
            log.warning("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON")
            return None

    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            log.warning("cannot read GOOGLE_SERVICE_ACCOUNT_FILE")
    return None


def is_configured() -> bool:
    return _library_available() and _credentials_info() is not None


def _service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    info = _credentials_info()
    if info is None:
        raise RuntimeError("No service-account credentials in the environment.")

    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=SCOPES
    )
    impersonate = os.environ.get("GOOGLE_DRIVE_IMPERSONATE")
    if impersonate:
        credentials = credentials.with_subject(impersonate)

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def folder_id_from_url(url: str) -> str:
    """Pull a folder id out of any of the shapes people paste."""
    import re

    if not url:
        return ""
    url = url.strip()
    patterns = [
        r"/folders/([A-Za-z0-9_-]{10,})",
        r"[?&]id=([A-Za-z0-9_-]{10,})",
        r"^([A-Za-z0-9_-]{20,})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return ""


def explain() -> dict:
    """Diagnostics for the System check page."""
    info = _credentials_info()
    impersonate = os.environ.get("GOOGLE_DRIVE_IMPERSONATE")

    state = {
        "library_installed": _library_available(),
        "credentials_present": info is not None,
        "service_account": (info or {}).get("client_email", ""),
        "project_id": (info or {}).get("project_id", ""),
        "impersonating": impersonate or "",
        "source": (
            "GOOGLE_SERVICE_ACCOUNT_JSON"
            if os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
            else ("GOOGLE_SERVICE_ACCOUNT_FILE"
                  if os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") else "")
        ),
    }

    if not state["library_installed"]:
        state["status"] = "unavailable"
        state["detail"] = (
            "The Google client libraries are not installed. "
            "Add google-api-python-client and google-auth to requirements.txt."
        )
    elif not state["credentials_present"]:
        state["status"] = "not configured"
        state["detail"] = (
            "Set GOOGLE_SERVICE_ACCOUNT_JSON to the service-account JSON. "
            "Submissions will still be stored on the server."
        )
    elif impersonate:
        state["status"] = "ready (impersonation)"
        state["detail"] = (
            f"Uploads are performed as {impersonate}, so files land in that "
            "person's own Drive and are owned by them. This requires "
            "domain-wide delegation for the service account."
        )
    else:
        state["status"] = "ready (shared drive required)"
        state["detail"] = (
            "A service account has no Drive storage of its own. Upload into a "
            "Shared drive the account is a member of, or set "
            "GOOGLE_DRIVE_IMPERSONATE to upload as the instructor instead."
        )
    return state


def check_folder(folder_url: str) -> dict:
    """Verify a pasted folder link before students rely on it."""
    folder_id = folder_id_from_url(folder_url)
    if not folder_id:
        return {"ok": False, "error": "That does not look like a Drive folder link."}
    if not is_configured():
        return {"ok": False, "error": "Google Drive is not configured on this server."}

    try:
        service = _service()
        meta = (
            service.files()
            .get(fileId=folder_id, fields="id,name,mimeType,driveId",
                 supportsAllDrives=True)
            .execute()
        )
    except Exception as exc:
        return {"ok": False, "error": _friendly(exc)}

    if meta.get("mimeType") != "application/vnd.google-apps.folder":
        return {"ok": False, "error": "That link points to a file, not a folder."}

    return {
        "ok": True,
        "name": meta.get("name", ""),
        "id": meta.get("id", ""),
        "shared_drive": bool(meta.get("driveId")),
        "note": (
            "This is a Shared drive folder — uploads will work."
            if meta.get("driveId")
            else "This is a personal-Drive folder. Uploads need "
                 "GOOGLE_DRIVE_IMPERSONATE set, or move it to a Shared drive."
        ),
    }


def _friendly(exc: Exception) -> str:
    text = str(exc)
    if "storageQuotaExceeded" in text:
        return (
            "Service accounts have no Drive storage. Use a Shared drive, or set "
            "GOOGLE_DRIVE_IMPERSONATE to upload as the instructor."
        )
    if "notFound" in text or "File not found" in text:
        return (
            "The folder was not found. Share it with the service account "
            "(Editor), then try again."
        )
    if "insufficientPermissions" in text or "forbidden" in text.lower():
        return "The service account does not have permission on that folder."
    if "unauthorized_client" in text:
        return (
            "Domain-wide delegation is not authorised for this service account "
            "and scope."
        )
    first = text.strip().splitlines()[0]
    return first[:300]


def upload_bytes(
    data: bytes,
    filename: str,
    mime_type: str,
    folder_url: str,
    description: str = "",
) -> DriveResult:
    """Upload straight into the instructor's folder, no manual step."""
    if not is_configured():
        return DriveResult(
            ok=False,
            error="Google Drive is not configured on this server.",
            hint="The file is stored on the server and can still be downloaded.",
        )

    folder_id = folder_id_from_url(folder_url)
    if not folder_id:
        return DriveResult(ok=False, error="No valid Drive folder is set for this task.")

    try:
        from googleapiclient.http import MediaIoBaseUpload

        service = _service()
        media = MediaIoBaseUpload(
            io.BytesIO(data),
            mimetype=mime_type or "application/octet-stream",
            resumable=len(data) > 5 * 1024 * 1024,
            chunksize=1024 * 1024,
        )
        created = (
            service.files()
            .create(
                body={
                    "name": filename,
                    "parents": [folder_id],
                    "description": description or "",
                },
                media_body=media,
                fields="id,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
    except Exception as exc:
        log.warning("drive upload failed for %s: %s", filename, exc)
        return DriveResult(ok=False, error=_friendly(exc))

    file_id = created.get("id", "")
    return DriveResult(
        ok=True,
        file_id=file_id,
        url=created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view",
    )

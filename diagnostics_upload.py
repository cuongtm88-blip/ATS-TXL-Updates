"""Secure, opt-in upload of ATS TXL diagnostic bundles to GitHub.

The token is supplied by the desktop application from the operating system
credential store.  It is deliberately never written to this module, a zip
file, the application settings file, or the public update repository.
"""
from __future__ import annotations

import base64
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests


GITHUB_API_VERSION = "2022-11-28"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
DEFAULT_FILES = (
    "summary.json",
    "browser-events.json",
    "windows-events.json",
    "windows-extended-diagnostics.json",
    "browser-processes.json",
)
OPTIONAL_FILES = ("onebss-error.png", "playwright-trace.zip")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class DiagnosticUploadError(RuntimeError):
    """A diagnostic package could not be uploaded safely."""


def validate_repository(repository):
    value = str(repository or "").strip()
    if not REPOSITORY_RE.fullmatch(value):
        raise DiagnosticUploadError("Repository GitHub phải có dạng owner/repository")
    return value


def _headers(token):
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "ATS-TXL-Diagnostics",
    }


def _put_contents(repository, remote_path, content, message, token, session=requests):
    response = session.put(
        f"https://api.github.com/repos/{repository}/contents/{remote_path}",
        headers=_headers(token),
        json={
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
        },
        timeout=(20, 90),
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code not in (200, 201):
        detail = payload.get("message") or f"HTTP {response.status_code}"
        raise DiagnosticUploadError(f"GitHub không nhận gói chẩn đoán: {detail}")
    return str((payload.get("content") or {}).get("html_url") or "")


def _archive(folder, include_screenshot=False, include_trace=False):
    folder = Path(folder)
    selected = list(DEFAULT_FILES)
    if include_screenshot:
        selected.append("onebss-error.png")
    if include_trace:
        selected.append("playwright-trace.zip")

    archive_path = folder.with_suffix(".upload.zip")
    skipped = []
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in selected:
            source = folder / name
            if source.is_file():
                archive.write(source, arcname=name)
            elif name in OPTIONAL_FILES:
                skipped.append(name)
    if archive_path.stat().st_size > MAX_UPLOAD_BYTES:
        archive_path.unlink(missing_ok=True)
        raise DiagnosticUploadError(
            "Gói chẩn đoán lớn hơn 25 MB; hãy tắt Playwright trace hoặc gửi thủ công."
        )
    return archive_path, skipped


def upload_folder(
    folder,
    repository,
    token,
    machine_id,
    *,
    include_screenshot=False,
    include_trace=False,
    session=requests,
):
    """Zip an allowlisted diagnostic folder and store it in a private repo."""
    repository = validate_repository(repository)
    token = str(token or "").strip()
    if not token:
        raise DiagnosticUploadError("Chưa cấu hình GitHub token chẩn đoán")
    folder = Path(folder)
    if not (folder / "summary.json").is_file():
        raise DiagnosticUploadError("Gói chẩn đoán không có summary.json")

    archive, skipped = _archive(folder, include_screenshot, include_trace)
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        remote_path = f"diagnostics/{machine_id}/{stamp}_{folder.name}.zip"
        url = _put_contents(
            repository,
            remote_path,
            archive.read_bytes(),
            f"ATS TXL diagnostic {folder.name}",
            token,
            session,
        )
    finally:
        archive.unlink(missing_ok=True)

    receipt = {
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "repository": repository,
        "remote_path": remote_path,
        "url": url,
        "skipped": skipped,
    }
    (folder / "github-upload.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return receipt


def upload_connection_test(repository, token, machine_id, session=requests):
    """Verify repository/token access without sending any OneBSS diagnostic."""
    repository = validate_repository(repository)
    payload = json.dumps(
        {
            "kind": "ATS TXL GitHub diagnostics connection test",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "machine_id": machine_id,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    remote_path = f"connection-tests/{machine_id}/{stamp}.json"
    url = _put_contents(
        repository,
        remote_path,
        payload,
        "ATS TXL diagnostic connection test",
        token,
        session,
    )
    return {"remote_path": remote_path, "url": url}

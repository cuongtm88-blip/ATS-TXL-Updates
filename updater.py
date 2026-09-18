"""Secure GitHub Releases updater used by the frozen Windows application."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import requests

from version import UPDATE_ASSET_NAME, UPDATE_REPOSITORY


GITHUB_API_VERSION = "2022-11-28"


class UpdateError(RuntimeError):
    """An update could not be checked, downloaded or installed safely."""


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    download_url: str
    checksum_url: str
    size: int
    notes: str = ""


def _version_key(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(value or "").strip())
    if not match:
        raise UpdateError(f'Phiên bản không hợp lệ: "{value}"')
    return tuple(int(part) for part in match.groups())


def is_newer_version(candidate, current):
    return _version_key(candidate) > _version_key(current)


def get_available_update(current_version, session=requests):
    """Return the latest newer release, or None when already up to date."""
    url = f"https://api.github.com/repos/{UPDATE_REPOSITORY}/releases/latest"
    response = session.get(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": f"ATS-TXL/{current_version}",
        },
        timeout=20,
    )
    if response.status_code == 404:
        # The repository exists but has no published release yet.
        return None
    try:
        payload = response.json()
    except ValueError as exc:
        raise UpdateError("GitHub trả về dữ liệu phiên bản không hợp lệ") from exc
    if not response.ok:
        description = payload.get("message") or f"HTTP {response.status_code}"
        raise UpdateError(f"Không kiểm tra được bản cập nhật: {description}")

    remote_version = str(payload.get("tag_name", "")).lstrip("vV")
    if not is_newer_version(remote_version, current_version):
        return None

    assets = payload.get("assets") or []
    executable = next(
        (asset for asset in assets if asset.get("name") == UPDATE_ASSET_NAME),
        None,
    )
    checksum_name = UPDATE_ASSET_NAME + ".sha256"
    checksum = next(
        (asset for asset in assets if asset.get("name") == checksum_name),
        None,
    )
    if not executable or not checksum:
        raise UpdateError(
            f"Release v{remote_version} thiếu {UPDATE_ASSET_NAME} hoặc {checksum_name}"
        )

    download_url = str(executable.get("browser_download_url", ""))
    checksum_url = str(checksum.get("browser_download_url", ""))
    expected_prefix = f"https://github.com/{UPDATE_REPOSITORY}/releases/download/"
    if not download_url.startswith(expected_prefix) or not checksum_url.startswith(
        expected_prefix
    ):
        raise UpdateError("GitHub trả về đường dẫn tài sản cập nhật không hợp lệ")

    return UpdateInfo(
        version=remote_version,
        download_url=download_url,
        checksum_url=checksum_url,
        size=int(executable.get("size") or 0),
        notes=str(payload.get("body") or "").strip(),
    )


def calculate_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_update(info, destination_dir, session=requests, progress=None):
    """Download the signed-by-hash release installer and verify it."""
    destination_dir = Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    partial = destination_dir / f"{UPDATE_ASSET_NAME}.{info.version}.part"
    downloaded = destination_dir / f"ATS-TXL-Setup-{info.version}.exe"

    try:
        response = session.get(
            info.download_url,
            headers={"User-Agent": f"ATS-TXL/{info.version}"},
            stream=True,
            timeout=(20, 600),
        )
        if not response.ok:
            raise UpdateError(f"Không tải được bản cập nhật: HTTP {response.status_code}")
        total = int(response.headers.get("Content-Length") or info.size or 0)
        received = 0
        with partial.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                output.write(chunk)
                received += len(chunk)
                if progress:
                    progress(received, total)
        if info.size and received != info.size:
            raise UpdateError(
                f"File cập nhật chưa tải đủ: {received}/{info.size} byte"
            )

        checksum_response = session.get(
            info.checksum_url,
            headers={"User-Agent": f"ATS-TXL/{info.version}"},
            timeout=20,
        )
        if not checksum_response.ok:
            raise UpdateError(
                f"Không tải được mã kiểm tra SHA-256: HTTP {checksum_response.status_code}"
            )
        match = re.search(r"\b([0-9a-fA-F]{64})\b", checksum_response.text)
        if not match:
            raise UpdateError("File SHA-256 trên GitHub không hợp lệ")
        expected = match.group(1).lower()
        actual = calculate_sha256(partial)
        if actual != expected:
            raise UpdateError("SHA-256 không khớp; đã hủy bản cập nhật không an toàn")

        os.replace(partial, downloaded)
        return downloaded
    except Exception:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def can_self_update():
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def _powershell_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def build_replacement_script(process_id, current_exe, new_exe, log_path):
    """Build the PowerShell script that swaps the EXE after this process exits."""
    current = _powershell_quote(Path(current_exe).resolve())
    new = _powershell_quote(Path(new_exe).resolve())
    backup = _powershell_quote(Path(str(Path(current_exe).resolve()) + ".old"))
    log = _powershell_quote(Path(log_path).resolve())
    return f"""$ErrorActionPreference = 'Stop'
$processId = {int(process_id)}
$currentExe = {current}
$newExe = {new}
$backupExe = {backup}
$logFile = {log}
try {{
    Wait-Process -Id $processId -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    if (Test-Path -LiteralPath $backupExe) {{ Remove-Item -LiteralPath $backupExe -Force }}
    Move-Item -LiteralPath $currentExe -Destination $backupExe -Force
    Move-Item -LiteralPath $newExe -Destination $currentExe -Force
    $newProcess = Start-Process -FilePath $currentExe -PassThru
    Start-Sleep -Seconds 10
    if ($newProcess.HasExited) {{ throw 'Ban cap nhat khong khoi dong on dinh' }}
    if (Test-Path -LiteralPath $backupExe) {{ Remove-Item -LiteralPath $backupExe -Force }}
}} catch {{
    $failure = $_.Exception.Message
    try {{
        if (Test-Path -LiteralPath $backupExe) {{
            if (Test-Path -LiteralPath $currentExe) {{ Remove-Item -LiteralPath $currentExe -Force }}
            Move-Item -LiteralPath $backupExe -Destination $currentExe -Force
            Start-Process -FilePath $currentExe
        }}
    }} catch {{}}
    Add-Content -LiteralPath $logFile -Value ((Get-Date).ToString('s') + ' ' + $failure)
}}
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
"""


def launch_windows_replacement(new_exe, work_dir):
    """Start a detached helper that replaces and relaunches the frozen EXE."""
    if not can_self_update():
        raise UpdateError("Tự thay EXE chỉ hoạt động trên bản đóng gói Windows")

    current_exe = Path(sys.executable).resolve()
    new_exe = Path(new_exe).resolve()
    if not new_exe.is_file():
        raise UpdateError("Không tìm thấy EXE cập nhật đã tải xuống")
    if new_exe == current_exe:
        raise UpdateError("EXE cập nhật trùng với ứng dụng đang chạy")
    if not os.access(current_exe.parent, os.W_OK):
        raise UpdateError(
            "Thư mục chứa ATS-TXL.exe không cho phép ghi. "
            "Hãy chuyển EXE sang thư mục của người dùng."
        )
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    script_path = work_dir / "install-update.ps1"
    log_path = work_dir / "update-error.log"
    script_path.write_text(
        build_replacement_script(os.getpid(), current_exe, new_exe, log_path),
        encoding="utf-8-sig",
    )

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            str(script_path),
        ],
        close_fds=True,
        creationflags=creationflags,
    )
    return script_path


def launch_windows_installer(installer_path):
    """Start the Inno Setup installer; it owns replacement of the onedir app."""
    if not can_self_update():
        raise UpdateError("Tự cập nhật chỉ hoạt động trong bản đóng gói Windows")
    installer_path = Path(installer_path).resolve()
    if not installer_path.is_file():
        raise UpdateError("Không tìm thấy bộ cài cập nhật đã tải xuống")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [str(installer_path), "/SP-", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"],
        close_fds=True,
        creationflags=creationflags,
    )

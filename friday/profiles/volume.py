"""Best-effort detection of whether a path sits on an encrypted volume.

Returns True / False when it can tell, None when it can't (e.g. inside a
container overlay). Detection is advisory; FRIDAY_VOLUME_ENCRYPTED_ASSERT lets
the owner vouch for a volume the checks can't see.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

ENCRYPTED_FUSE = {"fuse.gocryptfs", "fuse.cryfs", "fuse.encfs", "ecryptfs", "fuse.securefs"}


def _run(*args: str) -> str | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _existing_ancestor(path: Path) -> Path:
    path = path.resolve()
    while not path.exists():
        path = path.parent
    return path


def detect_encrypted(path: Path) -> tuple[bool | None, str]:
    path = _existing_ancestor(path)
    if sys.platform == "darwin":
        df = _run("df", "-P", str(path))
        if not df:
            return None, "df failed"
        mount = df.strip().splitlines()[-1].split()[-1]
        info = _run("diskutil", "info", "-plist", mount)
        if not info:
            return None, f"diskutil info failed for {mount}"
        data = plistlib.loads(info.encode())
        if data.get("FileVault") or data.get("Encryption") or data.get("Encrypted"):
            return True, f"{mount}: APFS/FileVault encryption on"
        return False, f"{mount}: not encrypted according to diskutil"
    if sys.platform.startswith("linux"):
        out = _run("findmnt", "-n", "-o", "FSTYPE,SOURCE", "--target", str(path))
        if not out:
            return None, "findmnt failed"
        fstype, _, source = out.strip().partition(" ")
        source = source.strip()
        if fstype in ENCRYPTED_FUSE:
            return True, f"{fstype} mount"
        if source.startswith("/dev/"):
            types = _run("lsblk", "-s", "-n", "-o", "TYPE", source) or ""
            if "crypt" in types.split():
                return True, f"{source} is on dm-crypt"
            return False, f"{source} has no crypt layer"
        return None, f"cannot inspect {fstype} {source}"
    return None, f"unsupported platform {sys.platform}"

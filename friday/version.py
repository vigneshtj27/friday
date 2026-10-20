"""Which code is running: friday/BUILD_INFO (written by deploy/gcp/deploy.sh ship), else git, else unknown."""

from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

BUILD_INFO = Path(__file__).with_name("BUILD_INFO")


@lru_cache(maxsize=1)
def build_info() -> dict[str, Any]:
    try:
        info = json.loads(BUILD_INFO.read_text())
        return {"sha": str(info.get("sha", ""))[:40], "short": str(info.get("sha", ""))[:7],
                "ref": info.get("ref"), "built_at": info.get("built_at"), "source": "build"}  # fmt: skip
    except (OSError, ValueError):
        pass
    try:
        sha = subprocess.run(["git", "-C", str(BUILD_INFO.parent), "rev-parse", "HEAD"], capture_output=True, text=True,
                             timeout=3, check=True).stdout.strip()  # fmt: skip
        dirty = subprocess.run(["git", "-C", str(BUILD_INFO.parent), "status", "--porcelain"], capture_output=True, text=True,
                               timeout=3).stdout.strip() != ""  # fmt: skip
        return {"sha": sha, "short": sha[:7] + ("-dirty" if dirty else ""), "ref": None, "built_at": None, "source": "git"}
    except (OSError, subprocess.SubprocessError):
        return {"sha": "", "short": "unknown", "ref": None, "built_at": None, "source": "unknown"}

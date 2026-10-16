"""Build docker/chrome-seccomp.json: Docker's default seccomp profile plus the namespace
syscalls Chrome's sandbox needs (clone with namespace flags, unshare, setns).

Much narrower than seccomp=unconfined or --cap-add SYS_ADMIN: everything else in
Docker's default profile still applies.

Usage: python scripts/gen_chrome_seccomp.py [path-to-moby-default.json]
(default: download from github.com/moby/profiles)
"""

import json
import sys
import urllib.request
from pathlib import Path

SRC = "https://raw.githubusercontent.com/moby/profiles/main/seccomp/default.json"
OUT = Path(__file__).resolve().parent.parent / "docker" / "chrome-seccomp.json"

if len(sys.argv) > 1:
    profile = json.loads(Path(sys.argv[1]).read_text())
else:
    with urllib.request.urlopen(SRC, timeout=30) as r:  # noqa: S310 - fixed https URL
        profile = json.load(r)

profile["syscalls"].append(
    {
        "names": ["clone", "unshare", "setns", "chroot"],
        "action": "SCMP_ACT_ALLOW",
        "comment": "friday: Chrome's namespace sandbox needs user/pid/net namespaces and chroot",
    }
)
OUT.parent.mkdir(exist_ok=True)
OUT.write_text(json.dumps(profile, indent=1) + "\n")
print(f"wrote {OUT}")

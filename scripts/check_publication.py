"""Fail CI if private runtime files or unpinned workflow actions enter Git."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


def main() -> int:
    paths = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
    failures = []
    for name in filter(None, paths):
        path = Path(name)
        if (
            path.name.startswith(".env")
            and path.name != ".env.example"
            or name == "config/accounts.yaml"
            or re.search(r"\.(?:sqlite|db)(?:[-.].*)?$", path.name)
            or path.suffix in {".log", ".pem", ".key", ".p12", ".pfx"}
            or name.startswith(("generated/", ".venv/", ".claude/"))
            or name.startswith("data/")
            and path.suffix == ".json"
            and not name.endswith(".example.json")
        ):
            failures.append(f"Private runtime file is tracked: {name}")
    for path in Path(".github/workflows").glob("*.yml"):
        content = path.read_text()
        if "pull_request_target:" in content:
            failures.append(f"Privileged pull-request trigger in {path}")
        for action in re.findall(r"uses:\s*([^\s#]+)", content):
            if not action.startswith("./") and not re.fullmatch(r"[^@]+@[0-9a-f]{40}", action):
                failures.append(f"Action is not pinned to a commit in {path}")
    for failure in failures:
        print(failure)
    if not failures:
        print("Publication file and workflow checks passed.")
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())

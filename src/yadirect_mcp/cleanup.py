"""Opt-in retention for regenerable JSON plans/audits; dry run by default.

Paid TSV exports, apply/repair journals, HTML reports, backups and subdirectories
are deliberately outside this command's scope.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


def prune(
    out_dir: Path, *, older_than_days: int = 30, keep_latest: int = 100, apply: bool = False,
) -> dict[str, Any]:
    if older_than_days < 1 or keep_latest < 1:
        raise ValueError("older_than_days и keep_latest должны быть положительными")
    root = out_dir.resolve(strict=True)
    cutoff = time.time() - older_than_days * 86400
    candidates = []
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file() or path.suffix != ".json":
            continue
        if not path.name.startswith(("plan_", "audit_")):
            continue
        resolved = path.resolve(strict=True)
        if resolved.parent != root:
            continue
        stat = path.stat()
        candidates.append((stat.st_mtime, stat.st_size, resolved))
    candidates.sort(key=lambda row: (row[0], str(row[2])), reverse=True)
    expired = [row for row in candidates[keep_latest:] if row[0] < cutoff]
    removed = []
    if apply:
        for mtime, size, path in expired:
            # Re-check containment and identity immediately before the single-file delete.
            if path.is_symlink() or path.resolve().parent != root:
                continue
            current = path.stat()
            if current.st_mtime != mtime or current.st_size != size:
                continue
            path.unlink()
            removed.append(str(path))
    return {
        "dry_run": not apply, "out_dir": str(root),
        "files": [str(row[2]) for row in expired], "bytes": sum(row[1] for row in expired),
        "removed": removed, "keep_latest": keep_latest, "older_than_days": older_than_days,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--older-than-days", type=int, default=30)
    parser.add_argument("--keep-latest", type=int, default=100)
    parser.add_argument(
        "--apply", action="store_true", help="Удалить файлы; без флага только список",
    )
    args = parser.parse_args()
    print(json.dumps(prune(
        args.out_dir, older_than_days=args.older_than_days,
        keep_latest=args.keep_latest, apply=args.apply,
    ), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

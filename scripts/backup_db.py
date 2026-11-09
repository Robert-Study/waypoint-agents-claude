#!/usr/bin/env python3
import argparse
import sqlite3
from datetime import datetime
from pathlib import Path
import sys


def backup(db_path: Path, out_dir: Path, keep: int = 14, now: datetime | None = None) -> Path:
    """Create a timestamped backup of a SQLite database using the online backup API.

    Args:
        db_path: Path to the source SQLite database (must exist).
        out_dir: Directory where the backup file will be placed.
        keep: Number of most recent backups to retain.
        now: Optional datetime to use for the backup filename (useful for testing).

    Returns:
        Path to the newly created backup file.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    backup_name = f"cc-{timestamp}.sqlite"
    backup_path = out_dir / backup_name

    # Perform online backup
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(backup_path))
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()

    # Prune old backups
    backups = sorted(out_dir.glob("cc-*.sqlite"))
    if len(backups) > keep:
        for old in backups[:-keep]:
            old.unlink()

    return backup_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[1]
    default_db = repo_root / "data" / "cc.sqlite"
    default_out = repo_root / "data" / "backups"

    parser = argparse.ArgumentParser(description="Backup a live SQLite DB.")
    parser.add_argument(
        "--db",
        type=Path,
        default=default_db,
        help="Path to the source SQLite database (default: %(default)s)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=default_out,
        help="Directory to store backups (default: %(default)s)",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=14,
        help="Number of recent backups to keep (default: %(default)s)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    backup_path = backup(args.db, args.out, keep=args.keep)

    size_kb = backup_path.stat().st_size // 1024
    # Count how many backups are now present
    kept = len(list(args.out.glob("cc-*.sqlite")))
    print(f"Backed up {backup_path.name} ({size_kb} KB), kept {kept}")


if __name__ == "__main__":
    main()

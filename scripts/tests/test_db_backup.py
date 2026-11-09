import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import importlib.util
_spec = importlib.util.spec_from_file_location('backup_db', Path(__file__).resolve().parents[1] / 'backup_db.py')
_m = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_m)
backup = _m.backup


@pytest.fixture
def sample_db(tmp_path: Path) -> Path:
    """Create a small SQLite DB with a single table and a few rows."""
    db_path = tmp_path / "cc.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE items (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                qty INTEGER NOT NULL
            );
            """
        )
        conn.executemany(
            "INSERT INTO items (name, qty) VALUES (?, ?);",
            [("apple", 10), ("banana", 20), ("cherry", 30)],
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def read_items(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    try:
        return list(conn.execute("SELECT id, name, qty FROM items ORDER BY id;"))
    finally:
        conn.close()


def test_db_backup_backup_creates_readable_copy(sample_db: Path, tmp_path: Path):
    out_dir = tmp_path / "backups"
    backup_path = backup(sample_db, out_dir, keep=5, now=datetime(2023, 1, 1, 12, 0, 0))

    # Verify file exists and is a valid SQLite DB with same data
    assert backup_path.is_file()
    assert read_items(backup_path) == read_items(sample_db)

    # Verify naming pattern
    assert backup_path.name.startswith("cc-20230101-120000")
    assert backup_path.suffix == ".sqlite"

    # Verify out_dir was created
    assert out_dir.is_dir()


def test_db_backup_pruning_keeps_newest(sample_db: Path, tmp_path: Path):
    out_dir = tmp_path / "prune_test"
    base_time = datetime(2023, 1, 1, 0, 0, 0)

    # Create 6 backups spaced one day apart
    for i in range(6):
        now = base_time + timedelta(days=i)
        backup(sample_db, out_dir, keep=10, now=now)

    # At this point there are 6 files
    assert len(list(out_dir.glob("cc-*.sqlite"))) == 6

    # Run backup with keep=3, new timestamp later than existing ones
    new_now = base_time + timedelta(days=10)
    backup(sample_db, out_dir, keep=3, now=new_now)

    backups = sorted(out_dir.glob("cc-*.sqlite"))
    # Should keep exactly 3 newest files
    assert len(backups) == 3
    # The newest should be the one just created
    assert backups[-1].name.startswith("cc-20230111-")
    # Ensure they are the three most recent timestamps
    expected_names = {
        f"cc-{(base_time + timedelta(days=d)).strftime('%Y%m%d-%H%M%S')}.sqlite"
        for d in (4, 5, 10)
    }
    assert {p.name for p in backups} == expected_names


def test_db_backup_out_dir_created_if_missing(sample_db: Path, tmp_path: Path):
    out_dir = tmp_path / "nested" / "dir" / "backups"
    # Directory does not exist yet
    assert not out_dir.exists()
    backup(sample_db, out_dir, keep=2, now=datetime(2023, 2, 2, 14, 30, 0))
    # After backup, directory should exist and contain one file
    assert out_dir.is_dir()
    assert len(list(out_dir.glob("cc-*.sqlite"))) == 1

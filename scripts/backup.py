"""Create a consistent SQLite online backup, including committed WAL contents."""

import argparse
import os
import sqlite3
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("database")
parser.add_argument("destination")
args = parser.parse_args()
if not Path(args.database).is_file() or Path(args.destination).exists():
    raise SystemExit("Source must exist and destination must be new")
source = sqlite3.connect(f"file:{Path(args.database).resolve()}?mode=ro", uri=True)
destination = sqlite3.connect(args.destination)
try:
    source.backup(destination)
finally:
    source.close()
    destination.close()
os.chmod(args.destination, 0o600)

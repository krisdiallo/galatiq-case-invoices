"""Create the required local inventory database without overwriting existing stock."""

import argparse
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[1] / "inventory.db"
SEED_INVENTORY = [("WidgetA", 15), ("WidgetB", 10), ("GadgetX", 5), ("FakeItem", 0)]


def setup_database(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS inventory (item TEXT PRIMARY KEY, stock INTEGER)"
        )
        connection.executemany(
            "INSERT OR IGNORE INTO inventory (item, stock) VALUES (?, ?)", SEED_INVENTORY
        )
    print(f"Inventory database ready: {db_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    setup_database(args.db_path)

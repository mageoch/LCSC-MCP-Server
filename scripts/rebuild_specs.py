#!/usr/bin/env python3
"""
Retroactively extract parametric specs from the existing LCSC parts database.

Usage:
    python scripts/rebuild_specs.py [--db PATH]
"""

import argparse
import sys
import time
from pathlib import Path

# Allow running from the project root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent))

from lcsc_mcp.db import PartsDB, _extract_specs, _component_type


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild component_specs table.")
    parser.add_argument("--db", default=None, help="Path to SQLite DB (default: data/lcsc_parts.db)")
    args = parser.parse_args()

    db = PartsDB(args.db)

    # Count total rows for progress display
    total = db._conn.execute("SELECT COUNT(*) FROM components").fetchone()[0]
    print(f"Database: {db.path}")
    print(f"Total components: {total:,}")
    print()

    # Fetch all rows in one shot (faster than streaming for a read-only pass)
    rows = db._conn.execute(
        "SELECT lcsc, description, category, subcategory FROM components"
    ).fetchall()

    spec_rows = []
    passives = 0
    errors = 0
    t0 = time.time()
    bar_width = 40

    for i, row in enumerate(rows, 1):
        try:
            s = _extract_specs(row[0], row[1], row[2], row[3])
            if s:
                spec_rows.append(s)
                passives += 1
        except Exception:
            errors += 1

        # Progress bar every 5000 rows or on the last row
        if i % 5000 == 0 or i == total:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0
            pct = i / total
            filled = int(bar_width * pct)
            bar = "█" * filled + "░" * (bar_width - filled)
            print(
                f"\r  [{bar}] {pct:5.1%}  "
                f"{i:>{len(str(total))},}/{total:,}  "
                f"{rate:,.0f} rows/s  "
                f"ETA {eta:4.0f}s  "
                f"passives: {passives:,}",
                end="",
                flush=True,
            )

    print()  # newline after progress bar
    print()

    # Commit to DB
    print("Writing to database…", end=" ", flush=True)
    db._conn.execute("DELETE FROM component_specs")
    if spec_rows:
        db._conn.executemany("""
            INSERT OR REPLACE INTO component_specs
                (lcsc, component_type, value_si, tolerance_pct,
                 voltage_v, current_a, power_w, dielectric)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, spec_rows)
    db._conn.commit()
    print("done.")
    print()

    elapsed = time.time() - t0
    print(f"Results")
    print(f"  Rows processed : {total:,}")
    print(f"  Passives found : {passives:,}  ({passives/total*100:.1f}%)")
    print(f"  Errors         : {errors}")
    print(f"  Elapsed        : {elapsed:.1f}s")
    print()

    # Breakdown by type
    for ctype in ("resistor", "capacitor", "inductor", "ferrite"):
        n = db._conn.execute(
            "SELECT COUNT(*) FROM component_specs WHERE component_type = ?", (ctype,)
        ).fetchone()[0]
        print(f"  {ctype:12}: {n:,}")

    print()
    print("component_specs table ready.")
    db.close()


if __name__ == "__main__":
    main()

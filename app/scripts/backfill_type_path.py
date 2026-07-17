"""Backfill component.type_path from names that already state the type.

Most of the inventory predates type_path, so 80 of 144 components carry none.
_type_prefix falls back to the name for label titles, but an unclassified part
still files under a generic CMP divider in a filing crate instead of RES or DIO,
and every type-aware query has to guess.

Follows the paths already in use on live data (plural: passives/resistors,
actives/diodes) rather than the singular tree in seed_taxonomy.py, which seeds
the unused Phase 2 component_taxonomy table and would introduce a second,
conflicting convention.

Dry run by default. Pass --apply to write.

    docker exec labinv_app python -m app.scripts.backfill_type_path
    docker exec labinv_app python -m app.scripts.backfill_type_path --apply
"""

import asyncio
import re
import sys

from sqlalchemy import select

from app.models.database import AsyncSessionLocal
from app.models.models import Component

# Ordered: first match wins. Anchored to the start of the name or a whole word,
# never a bare substring -- "RES" inside "PRESSURE" is not a resistor.
RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^res[-\s]", re.I), "passives/resistors"),
    (re.compile(r"^cap[-\s]?ti", re.I), "passives/capacitors/tantalum"),
    (re.compile(r"^cap[-\s]", re.I), "passives/capacitors"),
    (re.compile(r"^ind[-\s]", re.I), "passives/inductors"),
    (re.compile(r"\bled\b", re.I), "actives/diodes/led"),
    (re.compile(r"^1n[45]\d{3}", re.I), "actives/diodes"),
    (re.compile(r"\bzener\b", re.I), "actives/diodes/zener"),
    (re.compile(r"^(2n|bc|bd|tip)\d", re.I), "actives/transistors"),
    (re.compile(r"\b(mosfet|irf\d)", re.I), "actives/transistors/mosfet"),
    (re.compile(r"\b(atmega|attiny|esp32|esp8266|stm32|rp2040)", re.I), "actives/ic/microcontroller"),
    (re.compile(r"\b(header|jumper|dupont)\b", re.I), "connectors"),
    (re.compile(r"\b(crystal|oscillator|resonator)\b", re.I), "passives/crystals"),
]


def type_path_for(name: str | None) -> str | None:
    if not name:
        return None
    for pattern, path in RULES:
        if pattern.search(name):
            return path
    return None


async def main(apply: bool) -> None:
    async with AsyncSessionLocal() as db:
        components = (await db.execute(select(Component))).scalars().all()

        changes = []
        skipped_named = []
        for c in components:
            if c.type_path:
                continue  # never overwrite a classification already made
            path = type_path_for(c.name)
            if path:
                changes.append((c, path))
            else:
                skipped_named.append(c.name)

        total_missing = len(changes) + len(skipped_named)
        print(f"{len(components)} components, {total_missing} without type_path")
        print(f"  {len(changes)} can be classified from the name")
        print(f"  {len(skipped_named)} cannot -- left alone\n")

        by_path: dict[str, list[str]] = {}
        for c, path in changes:
            by_path.setdefault(path, []).append(c.name)
        for path, names in sorted(by_path.items()):
            print(f"  {path:34} {len(names):>3}  e.g. {names[0][:30]}")

        if skipped_named:
            print("\n  unclassifiable:")
            for n in skipped_named[:10]:
                print(f"    {n}")
            if len(skipped_named) > 10:
                print(f"    ... and {len(skipped_named) - 10} more")

        if not apply:
            print(f"\nDRY RUN -- nothing written. Re-run with --apply to set {len(changes)} rows.")
            return

        for c, path in changes:
            c.type_path = path
        await db.commit()
        print(f"\nAPPLIED: {len(changes)} components classified.")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))

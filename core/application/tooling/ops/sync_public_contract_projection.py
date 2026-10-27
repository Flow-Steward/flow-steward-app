#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile derived Field Catalog, Data Contracts, and Published Blocks from low-level registry sources."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="Compute projection diff without writing changes."
    )
    mode.add_argument(
        "--apply", action="store_true", help="Apply projection updates to the database."
    )
    args = parser.parse_args()

    from core.application.registry.public_contract_projection import (
        reconcile_public_contract_projection,
    )
    from core.application.registry.registry_manager import RegistryManager

    rm = RegistryManager.instance()
    if not rm.db_store:
        raise RuntimeError("Registry DB backend not configured")

    result = reconcile_public_contract_projection(rm.db_store, apply=bool(args.apply))
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

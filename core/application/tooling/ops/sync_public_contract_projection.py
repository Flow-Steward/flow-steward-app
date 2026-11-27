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
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--account",
        "--account-id",
        dest="account",
        default="",
        help=(
            "Account whose sources are reconciled (default: the installation account, "
            "stated in the output)."
        ),
    )
    scope.add_argument(
        "--all-accounts",
        action="store_true",
        help="Reconcile every account that owns a projection source, one after another.",
    )
    args = parser.parse_args()

    from core.application.registry.public_contract_projection import (
        reconcile_public_contract_projection,
    )
    from core.application.registry.registry_manager import RegistryManager
    from core.application.tooling.ops.account_option import (
        report_command_account,
        resolve_command_account,
    )

    rm = RegistryManager.instance()
    if not rm.db_store:
        raise RuntimeError("Registry DB backend not configured")

    if args.all_accounts:
        print("account: every account that owns a projection source", file=sys.stderr)
        result = reconcile_public_contract_projection(rm.db_store, apply=bool(args.apply))
    else:
        account = resolve_command_account(args)
        report_command_account(account)
        result = reconcile_public_contract_projection(
            rm.db_store, apply=bool(args.apply), account_id=account.account_id
        )
        result.update(account.as_payload())
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

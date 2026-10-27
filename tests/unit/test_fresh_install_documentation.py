"""Fresh-install operator docs must preserve the executable database order."""

from pathlib import Path


def test_fresh_install_upgrades_schema_before_bootstrap_seed() -> None:
    source = Path("docs/operations/checkout-reference.md").read_text(encoding="utf-8")
    upgrade = "./flow-steward database upgrade --author operator --maintenance-confirmed --confirm"
    bootstrap_seed = "./flow-steward database bootstrap-seed --author operator --confirm"

    assert source.count(upgrade) == 1
    assert source.count(bootstrap_seed) == 1
    assert source.index(upgrade) < source.index(bootstrap_seed)


def test_user_guide_leaves_fresh_install_seeding_to_the_installer() -> None:
    # 42b988338a: the installer checks the schema and applies seed data on both
    # profiles, so the quick start no longer runs either database command.
    guide = Path("docs/GUIDE.md").read_text(encoding="utf-8")

    assert "`./install.sh` checks the canonical schema and applies idempotent seed data" in guide
    assert "./flow-steward database bootstrap-seed --author operator --confirm" not in guide
    assert "Later SQL migrations use `database upgrade`" in guide
    assert "`--maintenance-confirmed --confirm`" in guide


def test_profile_switch_docs_keep_the_one_off_run_and_storage_checks() -> None:
    runbook = Path("docs/operations/compact-scalable-deployment.md").read_text(encoding="utf-8")

    assert "docker compose run --rm --no-deps" in runbook
    assert "app UID 10001 with artifact GID 10100" in runbook
    assert "reports `storage_connectivity` in the failed fields" in runbook


def test_profile_switch_docs_keep_retained_services_available_for_apply_and_recovery() -> None:
    runbook = Path("docs/operations/compact-scalable-deployment.md").read_text(encoding="utf-8")

    assert "restart the same PostgreSQL and RabbitMQ services" in runbook
    assert "stop only the old application runtimes and writers" in runbook
    assert (
        "Keep the retained PostgreSQL and RabbitMQ services running for `config apply`" in runbook
    )
    assert "run `flow-steward config apply` again" in runbook
    assert "remains stopped during recovery" in runbook
    assert "returns `runtime_restored=true`, retains the pending patch" in runbook
    assert "through the same disposable Compose mechanism" in runbook

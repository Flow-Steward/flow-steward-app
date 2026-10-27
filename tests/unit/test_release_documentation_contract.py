from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_install_and_update_docs_define_local_checkout_and_owner_only_token_contract() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs/GUIDE.md").read_text(encoding="utf-8")
    combined = readme + "\n" + guide

    assert "git clone <flow-steward-repo-url> flow-steward" in combined
    assert "does not require" in readme
    assert ".flow-steward/secret-input/github-release-token" in combined
    assert "Contents: read" in combined
    assert "chmod 700 .flow-steward/secret-input" in combined
    assert "chmod 600 .flow-steward/secret-input/github-release-token" in combined
    assert "https://docs.docker.com/engine/install/" in combined
    assert "https://docs.docker.com/engine/install/ubuntu/" in combined
    assert "https://docs.docker.com/desktop/" in combined


def test_release_token_is_not_documented_as_cli_or_environment_input() -> None:
    paths = (
        "README.md",
        "docs/GUIDE.md",
        "docs/operations/installation-clone.md",
        "docs/architecture/adr/0030-local-install-and-private-release-auth.md",
    )
    text = "\n".join((ROOT / path).read_text(encoding="utf-8") for path in paths)

    assert "--github-token" not in text
    assert "GITHUB_RELEASE_TOKEN=" not in text


def test_operator_docs_define_source_to_official_transition_journey() -> None:
    guide = (ROOT / "docs/GUIDE.md").read_text(encoding="utf-8")

    checkout_reference = (ROOT / "docs/operations/checkout-reference.md").read_text(
        encoding="utf-8"
    )
    for text in (checkout_reference, guide):
        normalized = " ".join(text.split())
        assert "source installation" in normalized
        assert "semantic ordering is unknown" in normalized
        assert "./flow-steward release check" in text
        assert "./flow-steward backup create" in text
        assert "./flow-steward maintenance enable --confirm" in text
        assert "./flow-steward release upgrade --confirm" in text
        assert "./flow-steward doctor" in text
        assert "./flow-steward maintenance disable --confirm" in text
        assert "resum" in text.lower()
        assert "official installed identity" in normalized


def test_clone_runbook_requires_official_transition_on_both_installations() -> None:
    text = (ROOT / "docs/operations/installation-clone.md").read_text(encoding="utf-8")

    assert "both the source and target" in text
    assert "./flow-steward release check" in text
    assert "./flow-steward release upgrade --confirm" in text
    assert "official-only" in text
    assert "target-local" in text
    assert "External S3 objects are not copied" in text


def test_local_install_adr_pins_origin_and_transition_contracts() -> None:
    text = (
        ROOT / "docs/architecture/adr/0030-local-install-and-private-release-auth.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    assert "source installed origin" in normalized
    assert "official installed origin" in normalized
    assert "semantic ordering is unknown" in normalized
    assert "reviewed and explicitly confirmed" in normalized
    assert "revalidated under the installer lifecycle lock" in normalized
    assert "installed-release.json" in text
    assert "official-only Clone prerequisite" in normalized

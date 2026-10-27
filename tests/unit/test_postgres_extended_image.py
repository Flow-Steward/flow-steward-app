from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_FROM = (
    "FROM pgvector/pgvector:0.8.5-pg17@"
    "sha256:d2ef61f42ef767baa5a1475393303cc235bcd92febd9d7014eddb48b41f3bad0"
)


def test_postgres_extended_image_pins_pgvector_tag_and_digest() -> None:
    dockerfile = (ROOT / "infra/docker/postgres-extended/Dockerfile").read_text(encoding="utf-8")

    assert EXPECTED_FROM in dockerfile
    assert "ARG PGVECTOR_BASE_IMAGE" not in dockerfile


def test_compose_and_installer_do_not_override_pgvector_base() -> None:
    paths = [
        ROOT / "infra/docker/docker-compose.yaml",
        ROOT / "infra/docker/docker-compose.prod.yaml",
        ROOT / "infra/docker/docker-compose.postgres-extended.yaml",
        *sorted((ROOT / "core/infrastructure/installer").glob("gateway*.py")),
    ]

    for path in paths:
        assert "PGVECTOR_BASE_IMAGE" not in path.read_text(encoding="utf-8")

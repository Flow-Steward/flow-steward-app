from tests.support.disposable_clone import CloneCommandHarness


def test_harness_streams_passphrase_only_on_stdin_and_never_in_argv(tmp_path) -> None:
    calls = []

    def run(argv, *, cwd, stdin):
        calls.append((tuple(argv), cwd, stdin))
        return {"status": "ok"}

    harness = CloneCommandHarness(run=run, source_root=tmp_path, target_root=tmp_path)
    harness.create(output_directory=tmp_path / "backups", passphrase=b"not-on-argv")
    harness.restore(
        archive=tmp_path / "source.fsclone",
        confirmation="target-installation",
        passphrase=b"also-not-on-argv",
    )

    assert calls[0][0] == (
        "flow-steward",
        "clone",
        "create",
        "--output-directory",
        str(tmp_path / "backups"),
        "--passphrase-stdin",
        "--confirm",
        "--json",
        "--non-interactive",
    )
    assert calls[0][2] == b"not-on-argv\n"
    assert "not-on-argv" not in " ".join(calls[0][0])
    assert calls[1][0][-4:] == (
        "--confirm",
        "target-installation",
        "--json",
        "--non-interactive",
    )
    assert calls[1][2] == b"also-not-on-argv\n"

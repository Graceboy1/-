"""Exercise the delivery tool with generated fixtures, never private files."""
import hashlib
from pathlib import Path
from zipfile import ZipFile

import pytest

from tools import make_release


@pytest.fixture
def release_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("Synthetic reviewer fixture", encoding="utf-8")
    (project / ".env.example").write_text("CONTEST_LLM_API_KEY=\n", encoding="utf-8")
    monkeypatch.setattr(make_release, "ROOT", project)
    monkeypatch.setattr(make_release, "FILES", ("README.md", ".env.example"))
    return project


def test_release_is_allowlisted_and_verifiable(release_project):
    # Private extras in the source tree must never be collected by recursion.
    (release_project / ".env").write_text("PRIVATE=not-for-release", encoding="utf-8")
    output = make_release.build_archive(release_project / "output" / "source.zip")
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == {"contest-edition/README.md", "contest-edition/.env.example", "contest-edition/RELEASE.sha256"}
        manifest = archive.read("contest-edition/RELEASE.sha256").decode()
        for line in manifest.splitlines():
            checksum, name = line.split("  ", 1)
            assert hashlib.sha256(archive.read("contest-edition/" + name)).hexdigest() == checksum
        assert archive.testzip() is None


@pytest.mark.parametrize("name", [".env", "runlogs/api.log", "private.sqlite3", ".git/config", "web/node_modules/file.js"])
def test_private_paths_are_rejected(release_project, monkeypatch, name):
    monkeypatch.setattr(make_release, "FILES", (name,))
    with pytest.raises(ValueError, match="Private/runtime"):
        make_release.build_archive(release_project / "source.zip")


def test_suspected_key_is_not_printed_or_packaged(release_project, capsys):
    secret = "sk" + "-" + "x" * 40
    (release_project / "README.md").write_text(secret, encoding="utf-8")
    with pytest.raises(ValueError, match="Potential credential") as error:
        make_release.build_archive(release_project / "source.zip")
    assert secret not in str(error.value)
    assert secret not in capsys.readouterr().out
    assert not (release_project / "source.zip").exists()


def test_frozen_archive_is_not_overwritten(release_project):
    output = make_release.build_archive(release_project / "source.zip")
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        make_release.build_archive(output)
    assert output.read_bytes() == original


def test_output_cannot_escape_project(release_project):
    with pytest.raises(ValueError, match="inside"):
        make_release.build_archive(release_project.parent / "outside.zip")

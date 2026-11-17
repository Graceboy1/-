"""Reviewer material assembly with disposable fixtures, never private files."""
from importlib.util import module_from_spec, spec_from_file_location
import hashlib
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from tools import make_release


@pytest.fixture
def assembly(tmp_path, monkeypatch):
    # Keep the CLI-compatible module import independent of developer sys.path.
    tools = Path(__file__).resolve().parents[1] / "tools"
    monkeypatch.syspath_prepend(str(tools))
    spec = spec_from_file_location("_submission_fixture_module", tools / "assemble_submission.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "project"
    (root / "docs").mkdir(parents=True)
    (root / "output" / "pdf").mkdir(parents=True)
    (root / "README.md").write_text("Generated public fixture", encoding="utf-8")
    (root / ".env.example").write_text("CONTEST_LLM_API_KEY=\n", encoding="utf-8")
    (root / "docs" / "technical.md").write_text("Generated technical description", encoding="utf-8")
    (root / "output" / "pdf" / "technical-20261117.pdf").write_bytes(b"%PDF-1.4\nGenerated fixture only")
    copies = {"docs/technical.md": "02_technical.md", "output/pdf/technical-20261117.pdf": "02_technical.pdf"}
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "COPIES", copies)
    monkeypatch.setattr(make_release, "ROOT", root)
    monkeypatch.setattr(make_release, "FILES", ("README.md", ".env.example", *copies))
    monkeypatch.setattr(module, "build_archive", make_release.build_archive)
    return module, root, copies


def paths(root, day="20261117"):
    folder = root / "output" / f"submission-{day}"
    source = folder / f"01_智慧交易助手-源码快照-{day}.zip"
    archive = root.parent / f"智慧交易助手-参赛材料包-{day}.zip"
    return folder, source, archive


def test_bundle_contains_only_explicit_materials_with_matching_copied_hashes(assembly):
    module, root, copies = assembly
    # Unrelated runtime files are deliberately not candidates for the bundle.
    (root / "private.sqlite3").write_bytes(b"PRIVATE-DO-NOT-PUBLISH")
    (root / "unselected.txt").write_text("Not an approved material", encoding="utf-8")
    module.main([])
    folder, source, archive = paths(root)
    assert source.is_file() and archive.is_file()
    for original, filename in copies.items():
        assert (folder / filename).read_bytes() == (root / original).read_bytes()
    checksums = (folder / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
    assert len(checksums) == len(copies) + 1
    for line in checksums:
        digest, filename = line.split("  ", 1)
        assert hashlib.sha256((folder / filename).read_bytes()).hexdigest() == digest
    with ZipFile(archive) as bundle:
        assert bundle.testzip() is None
        expected = {f"智慧交易助手-参赛材料/{name}" for name in [source.name, *copies.values(), "SHA256SUMS.txt"]}
        assert set(bundle.namelist()) == expected
        for filename in bundle.namelist():
            assert bundle.read(filename) == (folder / filename.rsplit("/", 1)[1]).read_bytes()
        with ZipFile(BytesIO(bundle.read(f"智慧交易助手-参赛材料/{source.name}"))) as inner:
            assert inner.testzip() is None
            assert not any("private" in name or "sqlite" in name for name in inner.namelist())
            manifest = inner.read("contest-edition/RELEASE.sha256").decode()
            for line in manifest.splitlines():
                digest, filename = line.split("  ", 1)
                assert hashlib.sha256(inner.read("contest-edition/" + filename)).hexdigest() == digest


@pytest.mark.parametrize("day", ["20260230", "20261301", "2026117", "../20261117", "２０２６１１１７", "2026-11-17", ""])
def test_invalid_dates_are_rejected_before_creating_materials(assembly, day):
    module, root, _ = assembly
    before = {path.relative_to(root) for path in root.rglob("*") if path.is_file()}
    with pytest.raises(SystemExit):
        module.main(["--date", day])
    assert {path.relative_to(root) for path in root.rglob("*") if path.is_file()} == before
    assert not list(root.parent.glob("*.zip"))


def test_new_date_selects_a_separate_snapshot_without_overwriting_previous(assembly):
    module, root, _ = assembly
    module.main(["--date", "20261117"])
    original = paths(root)[2].read_bytes()
    module.main(["--date", "20261118"])
    assert paths(root)[2].read_bytes() == original
    assert paths(root, "20261118")[2].is_file()


def test_existing_outer_archive_is_frozen_without_any_new_source_snapshot(assembly):
    module, root, _ = assembly
    folder, source, archive = paths(root)
    archive.write_bytes(b"previous-outer")
    with pytest.raises(SystemExit, match="already exists"):
        module.main([])
    assert archive.read_bytes() == b"previous-outer"
    assert not source.exists() and not folder.exists()


def test_existing_source_snapshot_is_never_overwritten_or_assembled(assembly):
    module, root, _ = assembly
    folder, source, archive = paths(root)
    folder.mkdir(parents=True)
    source.write_bytes(b"previous-source")
    with pytest.raises(SystemExit, match="source snapshot"):
        module.main([])
    assert source.read_bytes() == b"previous-source" and not archive.exists()
    assert [path.name for path in folder.iterdir()] == [source.name]


@pytest.mark.parametrize("filename", ["02_technical.md", "02_technical.pdf", "SHA256SUMS.txt"])
def test_any_frozen_material_conflict_is_checked_before_new_source_or_copies(assembly, filename):
    module, root, _ = assembly
    folder, source, archive = paths(root)
    folder.mkdir(parents=True)
    target = folder / filename
    target.write_bytes(b"previous-material")
    with pytest.raises(SystemExit, match="Preserve"):
        module.main([])
    assert target.read_bytes() == b"previous-material"
    assert not source.exists() and not archive.exists()
    assert [path.name for path in folder.iterdir()] == [filename]


def test_missing_pdf_does_not_create_source_or_outer_bundle(assembly):
    module, root, copies = assembly
    (root / "output" / "pdf" / "technical-20261117.pdf").unlink()
    with pytest.raises(FileNotFoundError):
        module.main([])
    folder, source, archive = paths(root)
    assert not source.exists() and not archive.exists()
    assert not any((folder / name).exists() for name in copies.values())


def test_material_source_cannot_escape_selected_project(assembly, monkeypatch):
    module, root, _ = assembly
    outside = root.parent / "outside-public-fixture.txt"
    outside.write_text("Not authorized for the selected project", encoding="utf-8")
    monkeypatch.setattr(module, "COPIES", {"../outside-public-fixture.txt": "unexpected.txt"})
    with pytest.raises(ValueError, match="Unexpected material path"):
        module.main([])
    folder, source, archive = paths(root)
    assert not source.exists() and not archive.exists() and not (folder / "unexpected.txt").exists()


@pytest.fixture
def verification(assembly, monkeypatch):
    from tools import verify_submission
    module, root, copies = assembly
    monkeypatch.setattr(verify_submission, "FILES", make_release.FILES)
    monkeypatch.setattr(verify_submission, "COPIES", copies, raising=False)
    module.main([])
    return verify_submission, root, copies


def rewrite_outer(path, *, remove=None, replace=None, extra=None, renew_hashes=False):
    """An attacker-controlled generated archive, never the real delivery."""
    with ZipFile(path) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    prefix = "智慧交易助手-参赛材料/"
    if remove:
        files.pop(prefix + remove)
    if replace:
        files.update({prefix + name: value for name, value in replace.items()})
    if extra:
        files.update({prefix + name: value for name, value in extra.items()})
    if renew_hashes:
        files[prefix+"SHA256SUMS.txt"] = "".join(
            f"{hashlib.sha256(value).hexdigest()}  {name[len(prefix):]}\n"
            for name, value in sorted(files.items()) if name != prefix+"SHA256SUMS.txt").encode()
    with ZipFile(path, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)


def test_readonly_verifier_accepts_exact_generated_source_and_outer_bundle(verification):
    verifier, root, copies = verification
    _, source, archive = paths(root)
    assert verifier.verify_source(source)["all_checksums_match"] is True
    result = verifier.verify_bundle(archive)
    assert result["materials"] == len(copies) + 2 and result["private_runtime_files"] == 0
    assert result["bundle_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()


def test_verifier_rejects_tampering_without_recomputed_checksum(verification):
    verifier, root, _ = verification
    archive = paths(root)[2]
    rewrite_outer(archive, replace={"02_technical.pdf": b"tampered fixture"})
    with pytest.raises(ValueError):
        verifier.verify_bundle(archive)


@pytest.mark.parametrize("missing", ["02_technical.pdf", "02_technical.md"])
def test_verifier_requires_every_approved_material_even_with_recomputed_hashes(verification, missing):
    verifier, root, _ = verification
    archive = paths(root)[2]
    rewrite_outer(archive, remove=missing, renew_hashes=True)
    with pytest.raises(ValueError):
        verifier.verify_bundle(archive)


def test_verifier_rejects_unapproved_outer_file_even_with_recomputed_hashes(verification):
    verifier, root, _ = verification
    archive = paths(root)[2]
    rewrite_outer(archive, extra={"unapproved.txt": b"must not be an approved material"}, renew_hashes=True)
    with pytest.raises(ValueError):
        verifier.verify_bundle(archive)


@pytest.mark.parametrize("filename", ["02_technical.pdf", "02_technical.md"])
def test_outer_copies_must_match_source_snapshot_not_just_self_signed_hashes(verification, filename):
    verifier, root, _ = verification
    archive = paths(root)[2]
    rewrite_outer(archive, replace={filename: b"different approved-copy content"}, renew_hashes=True)
    with pytest.raises(ValueError):
        verifier.verify_bundle(archive)


@pytest.mark.parametrize("filename", ["../escape.txt", "/absolute.txt", "C:/absolute.txt", "bad\\..\\escape.txt"])
def test_verifier_never_accepts_unsafe_archive_names(verification, filename):
    verifier, root, _ = verification
    unsafe = root / "unsafe-fixture.zip"
    with ZipFile(unsafe, "w") as archive:
        archive.writestr(filename, b"generated unsafe path")
    with pytest.raises(ValueError):
        verifier.contents(unsafe)


def test_verifier_rejects_duplicate_names_and_oversized_entries(verification, monkeypatch):
    verifier, root, _ = verification
    archive = root / "duplicate-fixture.zip"
    with pytest.warns(UserWarning), ZipFile(archive, "w") as output:
        output.writestr("same.txt", b"one")
        output.writestr("same.txt", b"two")
    with pytest.raises(ValueError):
        verifier.contents(archive)
    with ZipFile(archive, "w") as output:
        output.writestr("oversized.txt", b"12345")
    monkeypatch.setattr(verifier, "MAX_FILE", 4)
    with pytest.raises(ValueError):
        verifier.contents(archive)


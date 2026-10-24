import hashlib
import io
import json
import os
import subprocess

import pytest
from pydantic import ValidationError

from mini_deerflow.skills import (
    SkillContext,
    SkillsError,
    SkillsRegistry,
    default_skills_root,
)


def write_skill(root, name="example", *, body=b"Use evidence.", **overrides):
    metadata = {
        "id": name,
        "description": "Example skill",
        "applicability": "Research",
        "version": "1.0.0",
    }
    metadata.update(overrides)
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_bytes(b"---\n" + json.dumps(metadata).encode() + b"\n---\n" + body)
    return path


def test_samples_and_default_root():
    registry = SkillsRegistry()
    assert registry.root == default_skills_root()
    assert [m.id for m in registry.discover()] == [
        "deep-research",
        "research-brief",
        "slide-creation",
        "source-comparison",
    ]
    for metadata in registry.discover():
        assert registry.activate(metadata.id).description == metadata.description
    with pytest.raises(SkillsError, match="Conflicting"):
        registry.activate_many(["research-brief", "deep-research"])


def test_discovery_does_not_read_body(tmp_path, monkeypatch):
    path = write_skill(tmp_path, body=b"\xffnot UTF8 instructions")
    content = path.read_bytes()
    boundary = content.index(b"\n---\n") + len(b"\n---\n")

    class HeaderOnly(io.BytesIO):
        def read(self, size=-1):
            assert self.tell() + size <= boundary
            return super().read(size)

    registry = SkillsRegistry(tmp_path)
    monkeypatch.setattr(registry, "_open", lambda _: HeaderOnly(content))
    (metadata,) = registry.discover()
    assert metadata.id == "example"
    assert "instructions" not in type(metadata).model_fields


def test_activation_pins_content_and_metadata(tmp_path):
    path = write_skill(tmp_path)
    registry = SkillsRegistry(tmp_path)
    (record,) = registry.discover()
    context = registry.activate("example")
    assert context.fingerprint == hashlib.sha256(path.read_bytes()).hexdigest()
    assert context.instructions == "Use evidence."
    with pytest.raises(ValidationError):
        context.instructions = "changed"
    with pytest.raises(ValidationError):
        record.description = "changed"
    write_skill(tmp_path, body=b"Changed instructions.")
    assert registry.activate("example").instructions == "Changed instructions."
    assert context.instructions == "Use evidence."
    with pytest.raises(SkillsError, match="fingerprint"):
        registry.activate("example", expected_fingerprint=context.fingerprint)
    write_skill(tmp_path, description="changed metadata")
    with pytest.raises(SkillsError, match="metadata changed"):
        registry.activate("example")


@pytest.mark.parametrize(
    "selection",
    [
        "../example",
        "/example",
        "C:\\example",
        "\\\\?\\C:\\example",
        "example/SKILL.md",
        "EXAMPLE",
        "nul",
        "com1",
        "example.",
        "example ",
        "example:stream",
        "",
        "ex*",
        "./example",
    ],
)
def test_invalid_ids(tmp_path, selection):
    write_skill(tmp_path)
    with pytest.raises(SkillsError):
        SkillsRegistry(tmp_path).activate(selection)


@pytest.mark.parametrize(
    "overrides",
    [
        {"permissions": ["shell"]},
        {"tools": []},
        {"unknown": True},
        {"version": 1},
        {"version": "latest"},
        {"description": ""},
        {"applicability": []},
        {"conflicts": ["../escape"]},
        {"conflicts": ["example"]},
        {"conflicts": ["other", "other"]},
    ],
)
def test_invalid_metadata(tmp_path, overrides):
    write_skill(tmp_path, **overrides)
    with pytest.raises(SkillsError):
        SkillsRegistry(tmp_path).discover()


@pytest.mark.parametrize(
    "content",
    [
        b"no frontmatter",
        b"---\n{}",
        b"---\n[]\n---\nbody",
        b'---\n{"id":"a","id":"b"}\n---\nbody',
        b"---\nid: example\n---\nbody",
        b"---\n\xff\n---\nbody",
    ],
)
def test_malformed_frontmatter(tmp_path, content):
    path = write_skill(tmp_path)
    path.write_bytes(content)
    with pytest.raises(SkillsError):
        SkillsRegistry(tmp_path).discover()


def test_bounded_reads(tmp_path):
    write_skill(tmp_path, body=b"x" * 200)
    with pytest.raises(SkillsError, match="file exceeds"):
        SkillsRegistry(tmp_path, max_file_bytes=64).discover()
    with pytest.raises(SkillsError, match="frontmatter"):
        SkillsRegistry(tmp_path, max_frontmatter_bytes=16).discover()
    with pytest.raises(SkillsError, match="instructions exceed"):
        SkillsRegistry(tmp_path, max_instruction_bytes=10).activate("example")
    with pytest.raises(SkillsError):
        SkillsRegistry(tmp_path, max_file_bytes=True)


@pytest.mark.parametrize("body", [b"", b" \n ", b"\xff", b"hello\x00world"])
def test_invalid_instructions(tmp_path, body):
    write_skill(tmp_path, body=body)
    registry = SkillsRegistry(tmp_path)
    assert registry.discover()
    with pytest.raises(SkillsError):
        registry.activate("example")


def test_selection_order_duplicates_and_unknown(tmp_path):
    write_skill(tmp_path, "zeta")
    write_skill(tmp_path, "alpha")
    registry = SkillsRegistry(tmp_path)
    assert [x.id for x in registry.discover()] == ["alpha", "zeta"]
    assert [x.id for x in registry.activate_many(["zeta", "alpha"])] == [
        "alpha",
        "zeta",
    ]
    with pytest.raises(SkillsError, match="Duplicate"):
        registry.activate_many(["alpha", "alpha"])
    with pytest.raises(SkillsError, match="Unknown"):
        registry.activate("missing")
    with pytest.raises(SkillsError, match="Too many"):
        SkillsRegistry(tmp_path, max_skills=1).discover()


def test_id_mismatch_rejected(tmp_path):
    write_skill(tmp_path, id="different")
    with pytest.raises(SkillsError, match="match"):
        SkillsRegistry(tmp_path).discover()


def test_duplicate_catalog_id_rejected(tmp_path):
    write_skill(tmp_path, "alpha")
    write_skill(tmp_path, "zeta", id="alpha")
    with pytest.raises(SkillsError, match="Duplicate skill ID"):
        SkillsRegistry(tmp_path).discover()


def test_instruction_limit_counts_utf8_bytes(tmp_path):
    write_skill(tmp_path, body="é".encode() * 6)
    with pytest.raises(SkillsError, match="instructions exceed"):
        SkillsRegistry(tmp_path, max_instruction_bytes=10).activate("example")


def test_one_sided_conflicts(tmp_path):
    write_skill(tmp_path, "alpha", conflicts=["beta"])
    write_skill(tmp_path, "beta")
    with pytest.raises(SkillsError, match="Conflicting"):
        SkillsRegistry(tmp_path).activate_many(["beta", "alpha"])


@pytest.mark.parametrize("file_link", [False, True])
def test_link_escape_rejected(tmp_path, file_link):
    outside = tmp_path / "outside"
    target = write_skill(outside)
    root = tmp_path / "skills"
    root.mkdir()
    link = root / "example"
    if file_link:
        link.mkdir()
        link = link / "SKILL.md"
        destination = target
    else:
        destination = target.parent
    try:
        link.symlink_to(destination, target_is_directory=not file_link)
    except OSError:
        if os.name != "nt" or file_link:
            pytest.skip("Symlink creation not available")
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(destination)],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            pytest.skip("Junction creation not available")
    with pytest.raises(SkillsError, match="Symlinks|junction"):
        SkillsRegistry(root).discover()


def test_root_link_and_activation_link_swap(tmp_path):
    outside = tmp_path / "outside"
    write_skill(outside)
    root = tmp_path / "linked"
    try:
        root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation not available")
    with pytest.raises(SkillsError):
        SkillsRegistry(root).discover()
    local = tmp_path / "local"
    path = write_skill(local)
    registry = SkillsRegistry(local)
    registry.discover()
    path.unlink()
    path.symlink_to(outside / "example" / "SKILL.md")
    with pytest.raises(SkillsError):
        registry.activate("example")


def test_context_forbids_authority_fields():
    with pytest.raises(ValidationError):
        SkillContext(
            id="example",
            fingerprint="0" * 64,
            instructions="body",
            permissions=["shell"],
        )


def test_absent_root_is_empty(tmp_path):
    assert SkillsRegistry(tmp_path / "missing").discover() == ()

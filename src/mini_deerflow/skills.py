"""Repository-local, explicitly selected skills; no tool or permission grants.

SKILL.md uses UTF-8 JSON frontmatter between lines containing exactly ``---``.
Required keys: id, description, applicability, version. Optional: conflicts (IDs).
Discovery reads only bounded frontmatter, never the instruction body. Activation
checks the discovered metadata and pins the complete file's SHA-256 and body.

Consumers MUST treat instructions as subordinate to system/developer policies,
user intent and existing tool permissions. Declared conflicts and content pinning
are integrity safeguards, not a solution to prompt injection. Filesystem checks
assume the repository is not concurrently modified by a hostile local process.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
from pathlib import Path
from typing import Annotated, BinaryIO

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

SkillId = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=64)
]
ShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)
]
Fingerprint = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class SkillsError(ValueError):
    """Invalid, changed, conflicting, or unavailable repository skill."""


class _Frontmatter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: SkillId
    description: ShortText
    applicability: ShortText
    version: Annotated[
        str, StringConstraints(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", max_length=32)
    ]
    conflicts: tuple[SkillId, ...] = Field(default=(), max_length=32)


class SkillMetadata(_Frontmatter):
    """Immutable discovery record. Fingerprint hashes canonical metadata only."""

    fingerprint: Fingerprint


class SkillContext(BaseModel):
    """Immutable activation snapshot; instructions never confer authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: SkillId
    fingerprint: Fingerprint
    instructions: Annotated[str, StringConstraints(min_length=1, max_length=32_768)]
    description: ShortText | None = None


def default_skills_root() -> Path:
    """Source-checkout-local root, independent of the current working directory."""
    return Path(__file__).resolve().parents[2] / "skills"


def _valid_id(value: str) -> bool:
    if not isinstance(value, str) or len(value) > 64:
        return False
    if re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value) is None:
        return False
    return value.upper() not in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SkillsError(f"Duplicate frontmatter key: {key}")
        result[key] = value
    return result


class SkillsRegistry:
    """Fail-closed discovery and exact-ID activation of direct child skills.

    An absent root is an empty registry. Each child directory must be a valid ID
    and contain SKILL.md. Links/reparse points are rejected, including links that
    happen to point inside the root. Root paths are trusted caller configuration;
    IDs, never paths, are accepted by activation.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        max_frontmatter_bytes: int = 4096,
        max_file_bytes: int = 65_536,
        max_instruction_bytes: int = 32_768,
        max_skills: int = 128,
    ) -> None:
        for value in (
            max_frontmatter_bytes,
            max_file_bytes,
            max_instruction_bytes,
            max_skills,
        ):
            if type(value) is not int or value <= 0:
                raise SkillsError("Limits must be positive integers.")
        self.root = Path(root) if root is not None else default_skills_root()
        self._frontmatter_limit = max_frontmatter_bytes
        self._file_limit = max_file_bytes
        self._instruction_limit = min(max_instruction_bytes, 32_768)
        self._max_skills = max_skills
        self._metadata: dict[str, SkillMetadata] | None = None

    def _check_path(self, path: Path, *, directory: bool = False) -> os.stat_result:
        # Check all ancestors as well: resolve() alone would conceal root links.
        for component in (path, *path.parents):
            info = component.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise SkillsError(
                    "Symlinks and junction/reparse points are not allowed."
                )
        info = path.stat()
        if directory and not stat.S_ISDIR(info.st_mode):
            raise SkillsError("Skill root and child paths must be directories.")
        if not directory and not stat.S_ISREG(info.st_mode):
            raise SkillsError("Skill content must be a regular file.")
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise SkillsError("Skill path escapes the root.")
        return info

    def _open(self, path: Path) -> BinaryIO:
        info = self._check_path(path)
        if info.st_size > self._file_limit:
            raise SkillsError("Skill file exceeds the byte limit.")
        return path.open("rb", buffering=0)

    def _header(self, file: BinaryIO) -> _Frontmatter:
        # Unbuffered byte reads ensure discovery does not prefetch the body.
        header = bytearray()
        line = bytearray()
        while len(header) < self._frontmatter_limit:
            char = file.read(1)
            if not char:
                break
            header.extend(char)
            line.extend(char)
            if char == b"\n":
                if len(header) == len(line):
                    if bytes(line) not in (b"---\n", b"---\r\n"):
                        raise SkillsError(
                            "Expected JSON frontmatter delimited by --- lines."
                        )
                elif bytes(line) in (b"---\n", b"---\r\n"):
                    try:
                        text = bytes(header).decode("utf-8").splitlines()
                        data = json.loads(
                            "\n".join(text[1:-1]), object_pairs_hook=_no_duplicate_keys
                        )
                        if not isinstance(data, dict):
                            raise SkillsError("Frontmatter must be a JSON object.")
                        if "conflicts" in data:
                            if not isinstance(data["conflicts"], list):
                                raise SkillsError(
                                    "conflicts must be a JSON array of IDs."
                                )
                            data["conflicts"] = tuple(data["conflicts"])
                        metadata = _Frontmatter.model_validate(data)
                        if not _valid_id(metadata.id) or any(
                            not _valid_id(x) for x in metadata.conflicts
                        ):
                            raise SkillsError("Invalid or reserved skill ID.")
                        if metadata.id in metadata.conflicts or len(
                            set(metadata.conflicts)
                        ) != len(metadata.conflicts):
                            raise SkillsError(
                                "Self-conflicts and duplicate conflicts are invalid."
                            )
                        return metadata
                    except (ValueError, UnicodeError) as exc:
                        raise SkillsError(f"Invalid skill frontmatter: {exc}") from exc
                line.clear()
        raise SkillsError("Missing or oversized JSON frontmatter.")

    @staticmethod
    def _record(header: _Frontmatter) -> SkillMetadata:
        content = json.dumps(
            header.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return SkillMetadata(
            **header.model_dump(),
            fingerprint=hashlib.sha256(content.encode()).hexdigest(),
        )

    def discover(self) -> tuple[SkillMetadata, ...]:
        """Read only metadata; reject the entire catalog on malformed entries."""
        records: dict[str, SkillMetadata] = {}
        try:
            if not self.root.exists() and not self.root.is_symlink():
                self._metadata = records
                return ()
            self._check_path(self.root, directory=True)
            children = []
            for child in self.root.iterdir():
                if len(children) >= self._max_skills:
                    raise SkillsError("Too many skill entries.")
                children.append(child)
            for child in sorted(children, key=lambda p: p.name):
                if not _valid_id(child.name):
                    raise SkillsError("Invalid skill directory ID.")
                self._check_path(child, directory=True)
                with self._open(child / "SKILL.md") as file:
                    record = self._record(self._header(file))
                if record.id in records:
                    raise SkillsError(f"Duplicate skill ID: {record.id}")
                if record.id != child.name:
                    raise SkillsError("Skill ID must match its directory name.")
                records[record.id] = record
        except OSError as exc:
            raise SkillsError(f"Cannot discover skills: {exc}") from exc
        self._metadata = records
        return tuple(records[key] for key in sorted(records))

    def activate(
        self, skill_id: str, *, expected_fingerprint: str | None = None
    ) -> SkillContext:
        """Activate an exact ID, optionally requiring a previous full-file hash."""
        if not _valid_id(skill_id):
            raise SkillsError("Selection must be an exact, valid skill ID.")
        if self._metadata is None:
            self.discover()
        assert self._metadata is not None
        if skill_id not in self._metadata:
            raise SkillsError(f"Unknown skill ID: {skill_id}")
        try:
            path = self.root / skill_id / "SKILL.md"
            with self._open(path) as file:
                header = self._header(file)
                if self._record(header) != self._metadata[skill_id]:
                    raise SkillsError(
                        "Skill metadata changed since discovery; rediscover explicitly."
                    )
                file.seek(0)
                content = file.read(self._file_limit + 1)
            if len(content) > self._file_limit:
                raise SkillsError("Skill file exceeds the byte limit.")
            # Parse the same snapshot that is hashed, rather than a second read.
            snapshot = io.BytesIO(content)
            if self._header(snapshot) != header:
                raise SkillsError("Skill changed during activation.")
            body = snapshot.read()
            if len(body) > self._instruction_limit:
                raise SkillsError("Skill instructions exceed the byte limit.")
            instructions = body.decode("utf-8").strip()
            if not instructions or "\x00" in instructions:
                raise SkillsError(
                    "Skill instructions must be nonempty UTF-8 without NUL."
                )
            fingerprint = hashlib.sha256(content).hexdigest()
            if expected_fingerprint is not None and fingerprint != expected_fingerprint:
                raise SkillsError("Skill content fingerprint changed.")
            return SkillContext(
                id=skill_id,
                fingerprint=fingerprint,
                instructions=instructions,
                description=header.description,
            )
        except (OSError, UnicodeError) as exc:
            raise SkillsError(f"Cannot activate skill: {exc}") from exc

    def activate_many(
        self, skill_ids: list[str] | tuple[str, ...]
    ) -> tuple[SkillContext, ...]:
        """Deterministic, all-or-error selection; enforce either side's conflicts."""
        if any(not _valid_id(value) for value in skill_ids):
            raise SkillsError("Selection must contain exact skill IDs.")
        if len(set(skill_ids)) != len(skill_ids):
            raise SkillsError("Duplicate selected skill IDs.")
        contexts = tuple(self.activate(value) for value in sorted(skill_ids))
        assert self._metadata is not None or not contexts
        for context in contexts:
            if set(self._metadata[context.id].conflicts).intersection(skill_ids):
                raise SkillsError(f"Conflicting skill selection: {context.id}")
        return contexts

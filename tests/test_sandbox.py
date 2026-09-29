import hashlib
from pathlib import Path

import pytest

from mini_deerflow.persistence import InvalidThreadIdError
from mini_deerflow.sandbox import SessionSandboxResolver
from mini_deerflow.workspace import Workspace


def test_resolver_returns_stable_distinct_session_roots(tmp_path: Path) -> None:
    resolver = SessionSandboxResolver(tmp_path / "sandboxes")

    alpha_first = resolver.resolve("alpha")
    alpha_second = resolver.resolve("  alpha  ")
    beta = resolver.resolve("beta")
    alpha_key = f"v1-{hashlib.sha256(b'alpha').hexdigest()}"
    beta_key = f"v1-{hashlib.sha256(b'beta').hexdigest()}"

    assert alpha_first == tmp_path / "sandboxes" / "sessions" / alpha_key
    assert alpha_second == alpha_first
    assert beta == tmp_path / "sandboxes" / "sessions" / beta_key
    assert beta != alpha_first
    assert not resolver.base_root.exists()


def test_resolver_avoids_case_and_windows_device_name_collisions(
    tmp_path: Path,
) -> None:
    resolver = SessionSandboxResolver(tmp_path / "sandboxes")

    upper = resolver.resolve("Alpha")
    lower = resolver.resolve("alpha")
    windows_device = resolver.resolve("CON")
    trailing_dot = resolver.resolve("alpha.")

    assert len({upper, lower, windows_device, trailing_dot}) == 4
    assert all(path.name.startswith("v1-") for path in (upper, lower, windows_device))
    Workspace(windows_device).write_text("safe.txt", "safe")
    assert Workspace(windows_device).read_text("safe.txt") == "safe"


@pytest.mark.parametrize(
    "thread_id",
    [
        "",
        "../escape",
        "thread/slash",
        "thread\\backslash",
        "C:\\absolute",
        "a" * 129,
    ],
)
def test_resolver_rejects_invalid_thread_before_directory_creation(
    tmp_path: Path,
    thread_id: str,
) -> None:
    resolver = SessionSandboxResolver(tmp_path / "sandboxes")

    with pytest.raises(InvalidThreadIdError):
        resolver.resolve(thread_id)

    assert not resolver.base_root.exists()


def test_resolver_rejects_non_string_thread_id(tmp_path: Path) -> None:
    resolver = SessionSandboxResolver(tmp_path / "sandboxes")

    with pytest.raises(TypeError, match="thread_id"):
        resolver.resolve(123)  # type: ignore[arg-type]

    assert not resolver.base_root.exists()


def test_resolver_rejects_non_path_base_root() -> None:
    with pytest.raises(TypeError, match="base_root"):
        SessionSandboxResolver(123)  # type: ignore[arg-type]


@pytest.mark.parametrize("linked_component", ["sessions", "session"])
def test_resolver_rejects_linked_session_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    linked_component: str,
) -> None:
    resolver = SessionSandboxResolver(tmp_path / "sandboxes")
    expected_root = resolver.resolve("linked-thread")
    sessions_root = expected_root.parent
    original_is_symlink = Path.is_symlink

    def fake_is_symlink(path: Path) -> bool:
        linked_path = sessions_root if linked_component == "sessions" else expected_root
        return path == linked_path or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    with pytest.raises(ValueError, match="link or junction"):
        resolver.resolve("linked-thread")


def test_session_workspaces_isolate_the_same_relative_artifact(
    tmp_path: Path,
) -> None:
    resolver = SessionSandboxResolver(tmp_path / "sandboxes")
    alpha = Workspace(resolver.resolve("alpha"))
    beta = Workspace(resolver.resolve("beta"))

    alpha.write_text("reports/research-report.md", "alpha evidence")
    beta.write_text("reports/research-report.md", "beta evidence")

    assert alpha.read_text("reports/research-report.md") == "alpha evidence"
    assert beta.read_text("reports/research-report.md") == "beta evidence"
    assert alpha.list_files() == ("reports/research-report.md",)
    assert beta.list_files() == ("reports/research-report.md",)
    assert alpha.root != beta.root


def test_reopened_session_workspace_keeps_prior_artifacts(tmp_path: Path) -> None:
    sandbox_base = tmp_path / "sandboxes"
    first_resolver = SessionSandboxResolver(sandbox_base)
    first_workspace = Workspace(first_resolver.resolve("durable-thread"))
    first_workspace.write_text("notes/context.txt", "persisted context")

    reopened_resolver = SessionSandboxResolver(sandbox_base)
    reopened_workspace = Workspace(reopened_resolver.resolve("durable-thread"))

    assert reopened_workspace.root == first_workspace.root
    assert reopened_workspace.read_text("notes/context.txt") == "persisted context"

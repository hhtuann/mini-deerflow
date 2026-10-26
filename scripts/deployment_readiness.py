"""Fail-closed, credential-free Phase 3.4 local-deployment boundary audit.

Static source checks complement (not replace) real ASGI security tests.
No secrets, provider calls, network requests, file writes or public deployment.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

CHECK_NAMES = (
    "api_loopback_binding",
    "vite_loopback_binding",
    "api_trusted_hosts",
    "api_explicit_browser_origins",
    "request_body_limit",
    "locked_dependencies",
    "ignored_local_secrets",
    "no_tracked_env_credentials",
    "no_frontend_provider_credentials",
)


def _read(root: Path, relative: str) -> str:
    path = root / relative
    if (
        path.is_symlink()
        or not path.is_file()
        or not path.resolve().is_relative_to(root.resolve())
    ):
        raise ValueError("audit_source_missing_or_linked")
    return path.read_text(encoding="utf-8")


def _python_tree(root: Path, path: str) -> ast.Module:
    return ast.parse(_read(root, path), filename=path)


def _literal_assign(tree: ast.Module, symbol: str) -> object:
    for item in tree.body:
        if isinstance(item, (ast.Assign, ast.AnnAssign)):
            names = (
                [target.id for target in item.targets if isinstance(target, ast.Name)]
                if isinstance(item, ast.Assign)
                else [item.target.id]
                if isinstance(item.target, ast.Name)
                else []
            )
            if symbol in names:
                return ast.literal_eval(item.value)
    raise ValueError("required_configuration_missing")


def api_loopback_binding(root: Path) -> bool:
    tree = _python_tree(root, "src/mini_deerflow/api/__main__.py")
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "uvicorn"
        and node.func.attr == "run"
    ]
    if len(calls) != 1:
        return False
    keywords = {keyword.arg: keyword.value for keyword in calls[0].keywords}
    try:
        return (
            ast.literal_eval(keywords["host"]) == "127.0.0.1"
            and ast.literal_eval(keywords["port"]) == 8000
            and ast.literal_eval(keywords["factory"]) is True
            and "reload" not in keywords
        )
    except (ValueError, KeyError, TypeError):
        return False


def vite_loopback_binding(root: Path) -> bool:
    source = _read(root, "frontend/vite.config.ts")
    # Conservative check of the current intentionally simple literal Vite config.
    # Unknown/refactored syntax must be independently reviewed, not auto-approved.
    server = re.search(
        r"\bserver\s*:\s*\{(?P<settings>.*?)\btest\s*:", source, flags=re.DOTALL
    )
    if server is None:
        return False
    config = server.group("settings")
    return (
        re.search(r"\bhost\s*:\s*['\"]127\.0\.0\.1['\"]", config) is not None
        and re.search(r"\bport\s*:\s*5173\b", config) is not None
        and re.search(r"\bstrictPort\s*:\s*true\b", config) is not None
        and re.search(r"\btarget\s*:\s*['\"]http://localhost:8000['\"]", config)
        is not None
        and "0.0.0.0" not in config
        and "https://0.0.0.0" not in config
    )


def api_trusted_hosts(root: Path) -> bool:
    tree = _python_tree(root, "src/mini_deerflow/api/__init__.py")
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_middleware"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "TrustedHostMiddleware"
    ]
    if len(calls) != 1:
        return False
    try:
        args = {k.arg: ast.literal_eval(k.value) for k in calls[0].keywords}
        return args["allowed_hosts"] == ["localhost", "127.0.0.1", "testserver"]
    except (ValueError, KeyError, TypeError):
        return False


def api_explicit_browser_origins(root: Path) -> bool:
    tree = _python_tree(root, "src/mini_deerflow/api/security.py")
    try:
        origins = _literal_assign(tree, "ALLOWED_ORIGINS")
    except (ValueError, TypeError, KeyError):
        return False
    return origins == (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ) and "origin" in _read(root, "src/mini_deerflow/api/security.py")


def request_body_limit(root: Path) -> bool:
    tree = _python_tree(root, "src/mini_deerflow/api/security.py")
    try:
        size = _literal_assign(tree, "MAX_REQUEST_BYTES")
    except (ValueError, TypeError, KeyError):
        return False
    return size == 16_384 and "MAX_REQUEST_BYTES" in _read(
        root, "src/mini_deerflow/api/security.py"
    )


def locked_dependencies(root: Path) -> bool:
    return all(
        (root / name).is_file() and not (root / name).is_symlink()
        for name in ("uv.lock", "frontend/package-lock.json")
    )


def ignored_local_secrets(root: Path) -> bool:
    rules = _read(root, ".gitignore").splitlines()
    return all(item in rules for item in (".env", ".env.local", ".mini-deerflow/"))


def _tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--full-name", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        raise ValueError("git_index_unavailable")
    return [
        path.decode("utf-8", errors="strict")
        for path in result.stdout.split(b"\0")
        if path
    ]


def _secret_env_path(name: str) -> bool:
    path = PurePosixPath(name)
    if not path.name.startswith(".env"):
        return False
    if path.name in {".env.example", ".env.sample", ".env.template"}:
        return False
    return path.name == ".env" or path.name.startswith(".env.")


def no_tracked_env_credentials(root: Path) -> bool:
    return not any(_secret_env_path(path) for path in _tracked_files(root))


def no_frontend_provider_credentials(root: Path) -> bool:
    frontend = root / "frontend/src"
    if not frontend.is_dir() or frontend.is_symlink():
        return False
    for file in frontend.rglob("*"):
        if file.suffix not in {".ts", ".tsx", ".js", ".jsx"}:
            continue
        if file.is_symlink() or not file.is_file():
            return False
        source = file.read_text(encoding="utf-8")
        if re.search(
            r"\bMINI_DEERFLOW_API_KEY\b|"
            r"import\.meta\.env\.VITE_\w*(?:SECRET|TOKEN|API_KEY|PRIVATE_KEY)",
            source,
            flags=re.IGNORECASE,
        ):
            return False
    return True


def audit(root: Path) -> dict[str, object]:
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("invalid_repo_root")
    checks: dict[str, str] = {}
    for name in CHECK_NAMES:
        try:
            checks[name] = "PASS" if globals()[name](root) else "FAIL"
        except (
            OSError,
            ValueError,
            TypeError,
            UnicodeError,
            subprocess.TimeoutExpired,
        ):
            checks[name] = "BLOCKED"
    local_static = all(value == "PASS" for value in checks.values())
    return {
        "schema_version": 1,
        "status": "PASS_LOCAL_STATIC_BOUNDARY" if local_static else "FAIL_CLOSED",
        "checks": checks,
        "release_matrix": {
            "trusted_loopback_development": "READY" if local_static else "BLOCKED",
            "local_pilot_without_backup_restore_drill": "BLOCKED",
            "unauthenticated_network_deployment": "NOT_IN_SCOPE",
            "public_production": "BLOCKED",
        },
        "unverified_gates": [
            "human_backup_restore_drill",
            "production_secret_manager_and_rotation",
            "authenticated_tenant_isolation",
            "tls_reverse_proxy_and_public_ingress",
            "dependency_vulnerability_review",
            "power_loss_durability",
            "real_client_browser_and_office_manual_qa",
        ],
        "note": "Static tests never authorize public binding or verify live operations.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args(argv)
    try:
        report = audit(args.repo_root)
    except (OSError, ValueError):
        report = {"schema_version": 1, "status": "FAIL_CLOSED"}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS_LOCAL_STATIC_BOUNDARY" else 1


if __name__ == "__main__":
    raise SystemExit(main())

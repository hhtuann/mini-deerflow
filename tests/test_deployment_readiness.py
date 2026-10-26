"""Phase 3.4 local-security release gate and fail-closed regressions.

No external credentials or network are needed. Tests introspect real source
configuration and exercise the real FastAPI middleware via TestClient.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mini_deerflow.api import create_app
from scripts import deployment_readiness as readiness

ROOT = Path(__file__).resolve().parents[1]


def test_current_repo_passes_static_local_boundary_without_public_readiness():
    report = readiness.audit(ROOT)
    assert report["status"] == "PASS_LOCAL_STATIC_BOUNDARY"
    assert set(report["checks"]) == set(readiness.CHECK_NAMES)
    assert all(state == "PASS" for state in report["checks"].values())
    assert report["release_matrix"] == {
        "trusted_loopback_development": "READY",
        "local_pilot_without_backup_restore_drill": "BLOCKED",
        "unauthenticated_network_deployment": "NOT_IN_SCOPE",
        "public_production": "BLOCKED",
    }
    assert "authenticated_tenant_isolation" in report["unverified_gates"]


def test_security_gate_cli_is_read_only_and_redacted():
    result = subprocess.run(
        [sys.executable, "scripts/deployment_readiness.py", "--repo-root", str(ROOT)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=20,
        text=True,
    )
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["status"] == "PASS_LOCAL_STATIC_BOUNDARY"
    assert "api_key" not in result.stdout.lower()
    assert "token" not in result.stdout.lower()
    assert "127.0.0.1" not in result.stdout  # report contains only result labels


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("localhost", False),
        ("0.0.0.0", False),
        ("::", False),
        ("192.0.2.10", False),
    ],
)
def test_api_entrypoint_only_allows_explicit_loopback(host: str, expected: bool):
    import ast
    import tempfile

    assert readiness.api_loopback_binding(ROOT)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        entrypoint = root / "src/mini_deerflow/api/__main__.py"
        entrypoint.parent.mkdir(parents=True)
        content = (ROOT / "src/mini_deerflow/api/__main__.py").read_text(
            encoding="utf-8"
        )
        entrypoint.write_text(content.replace('"127.0.0.1"', f'"{host}"'))
        ast.parse(entrypoint.read_text())
        assert readiness.api_loopback_binding(root) is expected


@pytest.mark.parametrize(
    "override",
    ["true", "'::'", "process.env.VITE_HOST ?? true"],
)
def test_vite_duplicate_host_override_fails_gate(tmp_path: Path, override: str):
    # A second JS property wins at runtime; a presence-only regex must NOT
    # report loopback READY when a duplicate changes the effective host.
    source = (ROOT / "frontend/vite.config.ts").read_text(encoding="utf-8")
    target = tmp_path / "frontend/vite.config.ts"
    target.parent.mkdir(parents=True)
    target.write_text(
        source.replace(
            "host: '127.0.0.1',",
            f"host: '127.0.0.1', host: {override},",
        ),
        encoding="utf-8",
    )
    assert not readiness.vite_loopback_binding(tmp_path)


def test_shadowed_or_augmented_security_constants_fail_gate(tmp_path: Path):
    source = (ROOT / "src/mini_deerflow/api/security.py").read_text(encoding="utf-8")
    target = tmp_path / "src/mini_deerflow/api/security.py"
    target.parent.mkdir(parents=True)
    target.write_text(source + "\nMAX_REQUEST_BYTES = 1_000_000\n", encoding="utf-8")
    assert not readiness.request_body_limit(tmp_path)
    target.write_text(source + "\nALLOWED_ORIGINS = ('*',)\n", encoding="utf-8")
    assert not readiness.api_explicit_browser_origins(tmp_path)
    target.write_text(source + "\nALLOWED_ORIGINS += ('*',)\n", encoding="utf-8")
    assert not readiness.api_explicit_browser_origins(tmp_path)


@pytest.mark.parametrize(
    "secret_reference",
    [
        "import.meta.env.VITE_OPENAI_KEY",
        "import.meta.env['VITE_PROVIDER_TOKEN']",
        "process.env.OPENAI_API_KEY",
    ],
)
def test_additional_frontend_provider_secret_patterns_fail_gate(
    tmp_path: Path, secret_reference: str
):
    source = tmp_path / "frontend/src"
    source.mkdir(parents=True)
    (source / "App.tsx").write_text(
        f"export const leaked = {secret_reference};\n", encoding="utf-8"
    )
    assert not readiness.no_frontend_provider_credentials(tmp_path)


def test_missing_or_wildcard_trusted_hosts_fail_gate(tmp_path: Path):
    import_text = (ROOT / "src/mini_deerflow/api/__init__.py").read_text(
        encoding="utf-8"
    )
    path = tmp_path / "src/mini_deerflow/api/__init__.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        import_text.replace('["localhost", "127.0.0.1", "testserver"]', '["*"]'),
        encoding="utf-8",
    )
    assert readiness.api_trusted_hosts(ROOT)
    assert not readiness.api_trusted_hosts(tmp_path)


def test_wildcard_cors_and_missing_body_limit_fail_gate(tmp_path: Path):
    source = (ROOT / "src/mini_deerflow/api/security.py").read_text(encoding="utf-8")
    target = tmp_path / "src/mini_deerflow/api/security.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        source.replace('"http://localhost:5173",', '"*",'), encoding="utf-8"
    )
    assert not readiness.api_explicit_browser_origins(tmp_path)
    target.write_text(
        source.replace("MAX_REQUEST_BYTES = 16_384", "MAX_REQUEST_BYTES = 1_000_000"),
        encoding="utf-8",
    )
    assert not readiness.request_body_limit(tmp_path)


def test_vite_public_bind_and_proxy_target_are_blocked(tmp_path: Path):
    source = (ROOT / "frontend/vite.config.ts").read_text(encoding="utf-8")
    target = tmp_path / "frontend/vite.config.ts"
    target.parent.mkdir(parents=True)
    target.write_text(source.replace("'127.0.0.1'", "'0.0.0.0'"))
    assert not readiness.vite_loopback_binding(tmp_path)
    target.write_text(
        source.replace("'http://localhost:8000'", "'https://public.example'")
    )
    assert not readiness.vite_loopback_binding(tmp_path)


@pytest.mark.parametrize(
    "file",
    [
        ".env",
        ".env.production",
        "frontend/.env",
        "frontend/.env.local",
    ],
)
def test_sensitive_env_names_are_not_allowed_in_git_index(file: str):
    assert readiness._secret_env_path(file)


@pytest.mark.parametrize(
    "file",
    [
        ".env.example",
        "frontend/.env.sample",
        "frontend/.env.template",
        "README.md",
        "uv.lock",
    ],
)
def test_documented_templates_and_nonenvironment_files_are_allowed(file: str):
    assert not readiness._secret_env_path(file)


def test_frontend_import_of_provider_secret_fails_closed(tmp_path: Path):
    source = tmp_path / "frontend/src"
    source.mkdir(parents=True)
    (source / "App.tsx").write_text(
        "const secret = import.meta.env.VITE_PROVIDER_API_KEY;\n",
        encoding="utf-8",
    )
    assert not readiness.no_frontend_provider_credentials(tmp_path)
    (source / "App.tsx").write_text(
        "const secret = import.meta.env.VITE_PROVIDER_SECRET;\n",
        encoding="utf-8",
    )
    assert not readiness.no_frontend_provider_credentials(tmp_path)
    (source / "App.tsx").write_text("export const ok = 1;\n", encoding="utf-8")
    assert readiness.no_frontend_provider_credentials(tmp_path)


def test_runtime_host_and_origin_rejections_fail_closed(tmp_path: Path):
    with TestClient(create_app(tmp_path)) as client:
        assert (
            client.get("/api/health", headers={"Host": "attacker.test"}).status_code
            == 400
        )
        for origin in ("https://attacker.test", "http://localhost:5173.attacker.test"):
            response = client.post(
                "/api/offline/conversations",
                headers={"Origin": origin},
                json={"message": "Inspect the allowed local request boundary."},
            )
            assert response.status_code == 403
            assert "access-control-allow-origin" not in response.headers
        response = client.options(
            "/api/offline/conversations",
            headers={
                "Origin": "https://attacker.test",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in response.headers


def test_release_gate_fails_closed_on_empty_directory(tmp_path: Path):
    report = readiness.audit(tmp_path)
    assert report["status"] == "FAIL_CLOSED"
    assert all(state in {"BLOCKED", "FAIL"} for state in report["checks"].values())
    assert report["release_matrix"]["public_production"] == "BLOCKED"
    assert report["release_matrix"]["trusted_loopback_development"] == "BLOCKED"

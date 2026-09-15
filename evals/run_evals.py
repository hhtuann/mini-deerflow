"""Run the deterministic final-MVP invariant suite without external services."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def _load_dataset(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("evaluation dataset must use schema_version 1")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("evaluation dataset must contain cases")
    return payload


def _run_case(repo_root: Path, case: dict[str, object]) -> dict[str, object]:
    case_id = case.get("id")
    node_id = case.get("test")
    invariants = case.get("invariants")
    if (
        not isinstance(case_id, str)
        or not isinstance(node_id, str)
        or not isinstance(invariants, list)
        or not all(isinstance(item, str) for item in invariants)
    ):
        raise ValueError("each evaluation case needs id, test, and invariants")

    case_temp_root = (
        repo_root
        / ".mini-deerflow"
        / "eval-tmp"
        / hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:16]
    )
    case_temp_root.parent.mkdir(parents=True, exist_ok=True)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            node_id,
            "-q",
            "--tb=no",
            "-p",
            "no:cacheprovider",
            f"--basetemp={case_temp_root}",
        ],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    passed = completed.returncode == 0
    return {
        "id": case_id,
        "passed": passed,
        "invariants": len(invariants),
        "passed_invariants": len(invariants) if passed else 0,
        "failure_code": None if passed else "invariant_test_failed",
    }


def evaluate(dataset_path: Path) -> dict[str, object]:
    repo_root = Path(__file__).resolve().parents[1]
    dataset = _load_dataset(dataset_path)
    cases = dataset["cases"]
    assert isinstance(cases, list)
    results = [_run_case(repo_root, case) for case in cases]
    passed_cases = sum(bool(result["passed"]) for result in results)
    invariant_count = sum(int(result["invariants"]) for result in results)
    passed_invariants = sum(int(result["passed_invariants"]) for result in results)
    case_count = len(results)
    return {
        "schema_version": 1,
        "suite": dataset["suite"],
        "metrics": {
            "cases_total": case_count,
            "cases_passed": passed_cases,
            "cases_failed": case_count - passed_cases,
            "case_pass_rate": passed_cases / case_count,
            "invariants_total": invariant_count,
            "invariants_passed": passed_invariants,
            "invariant_pass_rate": passed_invariants / invariant_count,
        },
        "results": results,
        "limitations": dataset["limitations"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic Mini DeerFlow contract evaluations.",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(__file__).with_name("dataset.json"),
    )
    arguments = parser.parse_args(argv)
    try:
        report = evaluate(arguments.dataset.resolve())
    except (OSError, ValueError, json.JSONDecodeError):
        print("Evaluation failed: invalid or unavailable dataset.", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    metrics = report["metrics"]
    assert isinstance(metrics, dict)
    return 0 if metrics["cases_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

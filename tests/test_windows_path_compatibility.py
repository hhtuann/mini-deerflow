"""Stdlib-only compatibility checks, runnable without project dependencies."""

import os
import tempfile
import unittest
from pathlib import Path, PurePath
from unittest.mock import Mock, patch

from mini_deerflow import workspace as workspace_module
from mini_deerflow.workspace import Workspace, WorkspacePathError

INVALID_COMPONENTS = (
    "NUL",
    "con.txt",
    "CON .txt",
    "AUX",
    "prn.log",
    "CONIN$",
    "CONOUT$.txt",
    "COM1",
    "COM9.txt",
    "LPT1",
    "lpt9.log",
    "COM¹",
    "LPT¹",
    "COM²",
    "LPT².txt",
    "COM³",
    "LPT³.log",
    "report.md.",
    "report.md ",
    *(f"report{char}name.md" for char in '*?"<>|:'),
    *(f"report{chr(code)}name.md" for code in range(32)),
)
VALID_COMPONENTS = (
    "report.md",
    "day-15-demo-turn-second.md",
    "COM0.txt",
    "COM10.txt",
    "LPT0",
    "LPT10",
    "console.md",
    "auxiliary.md",
    ".NUL",
    "bằng-chứng.md",
)


class ReservedComponentCompatibilityTests(unittest.TestCase):
    def test_python312_fallback_rejects_reserved_components(self) -> None:
        with (
            patch.object(os.path, "isreserved", None, create=True),
            patch.object(
                PurePath, "is_reserved", side_effect=AssertionError, create=True
            ),
        ):
            for component in INVALID_COMPONENTS:
                with self.subTest(component=repr(component)):
                    self.assertTrue(
                        workspace_module._is_reserved_windows_component(component)
                    )
            for component in VALID_COMPONENTS:
                with self.subTest(component=component):
                    self.assertFalse(
                        workspace_module._is_reserved_windows_component(component)
                    )

    def test_prefers_available_native_api(self) -> None:
        native = Mock(side_effect=[True, False])
        with (
            patch.object(os.path, "isreserved", native, create=True),
            patch.object(
                PurePath, "is_reserved", side_effect=AssertionError, create=True
            ),
        ):
            self.assertTrue(
                workspace_module._is_reserved_windows_component("future-reserved.md")
            )
            self.assertFalse(workspace_module._is_reserved_windows_component("NUL"))
        native.assert_any_call("future-reserved.md")
        native.assert_any_call("NUL")
        self.assertEqual(native.call_count, 2)

    @unittest.skipUnless(
        hasattr(os.path, "isreserved"), "Native API requires Windows Python 3.13+"
    )
    def test_real_native_api_agrees_with_python312_fallback(self) -> None:
        native = os.path.isreserved
        with patch.object(os.path, "isreserved", None):
            for component in (*INVALID_COMPONENTS, *VALID_COMPONENTS):
                with self.subTest(component=repr(component)):
                    self.assertEqual(
                        workspace_module._is_reserved_windows_component(component),
                        native(component),
                    )


@unittest.skipUnless(os.name == "nt", "Windows workspace filename validation")
class WindowsWorkspaceCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="p0w-")
        self.addCleanup(directory.cleanup)
        self.workspace = Workspace(Path(directory.name) / "workspace")

    def test_invalid_components_rejected_before_filesystem_resolution(self) -> None:
        for component in INVALID_COMPONENTS:
            with self.subTest(component=repr(component)):
                with self.assertRaises(WorkspacePathError):
                    self.workspace.resolve_path(f"reports/{component}")
                with self.assertRaises(WorkspacePathError):
                    self.workspace.resolve_path(f"{component}/report.md")
        self.assertEqual(self.workspace.list_files(), ())

    def test_ordinary_artifacts_accepted_without_deprecated_api(self) -> None:
        with patch.object(
            PurePath, "is_reserved", side_effect=AssertionError, create=True
        ):
            for component in VALID_COMPONENTS:
                with self.subTest(component=component):
                    relative = f"reports/{component}"
                    self.workspace.write_text(relative, component)
                    self.assertEqual(self.workspace.read_text(relative), component)
        self.assertEqual(len(self.workspace.list_files()), len(VALID_COMPONENTS))

    def test_relative_normalization_preserves_containment(self) -> None:
        self.assertEqual(self.workspace.resolve_path("."), self.workspace.root)
        self.assertEqual(
            self.workspace.resolve_path("nested/../report.md"),
            self.workspace.root / "report.md",
        )
        for relative in ("../outside.md", r"..\outside.md"):
            with self.subTest(relative=relative), self.assertRaises(WorkspacePathError):
                self.workspace.resolve_path(relative)


@unittest.skipUnless(os.name == "posix", "Actual POSIX filesystem semantics")
class PosixWorkspaceCompatibilityTests(unittest.TestCase):
    def test_windows_rules_do_not_restrict_posix_names(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p0p-") as directory:
            workspace = Workspace(Path(directory) / "workspace")
            with patch.object(
                os.path, "isreserved", side_effect=AssertionError, create=True
            ):
                for name in (
                    "NUL",
                    "report*.md",
                    "report:stream",
                    "trailing.",
                    "trailing ",
                ):
                    with self.subTest(name=name):
                        workspace.write_text(name, name)
                        self.assertEqual(workspace.read_text(name), name)


if __name__ == "__main__":
    unittest.main()

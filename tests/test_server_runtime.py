import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _install_test_stubs() -> None:
    grpc_mod = types.ModuleType("grpc")
    grpc_mod.server = lambda *args, **kwargs: None
    sys.modules.setdefault("grpc", grpc_mod)

    jwt_mod = types.ModuleType("jwt")
    jwt_mod.encode = lambda payload, private_key, algorithm=None: "test-jwt"
    sys.modules.setdefault("jwt", jwt_mod)

    cap_pb2 = types.ModuleType("capability_pb2")

    class _Msg:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    cap_pb2.HealthResponse = _Msg
    cap_pb2.InvokeResponse = _Msg
    cap_pb2.InvokeChunk = _Msg
    sys.modules.setdefault("capability_pb2", cap_pb2)

    cap_pb2_grpc = types.ModuleType("capability_pb2_grpc")
    cap_pb2_grpc.CapabilityServicer = object
    cap_pb2_grpc.add_CapabilityServicer_to_server = lambda servicer, server: None
    sys.modules.setdefault("capability_pb2_grpc", cap_pb2_grpc)


SERVER_DIR = Path(__file__).resolve().parents[1] / "capabilities" / "coding-github" / "container"
sys.path.insert(0, str(SERVER_DIR))
_install_test_stubs()

import server  # noqa: E402


class ServerRuntimePolicyTests(unittest.TestCase):
    def test_available_checks_are_repo_and_toolchain_aware(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n", encoding="utf-8")

            with mock.patch.object(server, "_command_exists", side_effect=lambda b: b == "cargo"):
                info = server._available_checks_with_reasons(repo)

            self.assertIn("cargo check", info["available"])
            self.assertNotIn("npm test", info["available"])

    def test_install_toolchain_rejects_non_allowlisted_package(self) -> None:
        with self.assertRaises(server.ToolError):
            server._validate_toolchain_packages("pip", ["not-allowlisted-package"])

    def test_install_toolchain_runs_user_space_pip_install(self) -> None:
        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(server, "TOOLCHAIN_ROOT", Path(td) / ".selu-tools"):
                with mock.patch.object(server, "INSTALL_AUDIT_FILE", Path(td) / "install-audit.jsonl"):
                    with mock.patch.object(server, "_run_command", side_effect=fake_run):
                        result = server.handle_install_toolchain(
                            {"manager": "pip", "packages": ["pytest"]},
                            {},
                            "thread-install",
                        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["manager"], "pip")
        self.assertEqual(result["installed_count"], 1)

    def test_toolchain_probe_includes_probe_sections(self) -> None:
        def fake_probe(binary: str):
            return {"binary": binary, "found": binary == "git", "path": "/usr/bin/git" if binary == "git" else None}

        with mock.patch.object(server, "_probe_binary", side_effect=fake_probe):
            result = server.handle_toolchain_probe({}, {}, "thread-probe")

        self.assertTrue(result["ok"])
        self.assertIn("supported_install_managers", result)
        self.assertIn("package_allowlist", result)
        self.assertIn("toolchain_dirs", result)
        self.assertIn("binaries", result)
        self.assertIn("lsp_servers", result)
        self.assertIn("path_hints", result)


if __name__ == "__main__":
    unittest.main()

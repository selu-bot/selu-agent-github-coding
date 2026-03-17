from __future__ import annotations

import base64
import json
import logging
import os
import re
import select
import shlex
import subprocess
import threading
import time
from concurrent import futures
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import grpc
import jwt

import capability_pb2
import capability_pb2_grpc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("coding-github")

WORKSPACE_ROOT = Path(os.environ.get("SELU_WORKSPACE_ROOT", "/workspace")).resolve()
STATE_FILE = WORKSPACE_ROOT / ".selu-coding" / "state.json"
DEFAULT_THREAD_ID = "__default__"
MAX_TOOL_OUTPUT = 12000
GITHUB_API_VERSION = "2022-11-28"

REPO_SHORTHAND_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REPO_URL_RE = re.compile(r"^https://github\\.com/([^/]+)/([^/]+?)(?:\\.git)?/?$")

TOOL_PRIMARY_PARAM = {
    "open_repository": "repository",
    "create_feature_branch": "feature",
    "read_file": "path",
    "search_text": "query",
    "write_file": "path",
    "replace_in_file": "path",
    "write_files": "files",
    "lsp_definition": "path",
    "lsp_references": "path",
    "install_toolchain": "manager",
    "commit_changes": "message",
    "create_pull_request": "title",
}

LSP_SERVER_BY_LANGUAGE = {
    "rust": ["rust-analyzer"],
    "python": ["pylsp"],
    "go": ["gopls"],
    "typescript": ["typescript-language-server", "--stdio"],
    "javascript": ["typescript-language-server", "--stdio"],
    "java": ["jdtls"],
    "kotlin": ["kotlin-language-server"],
    "bash": ["bash-language-server", "start"],
    "yaml": ["yaml-language-server", "--stdio"],
}

LANGUAGE_IDS_BY_EXTENSION = {
    ".rs": "rust",
    ".py": "python",
    ".go": "go",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".yaml": "yaml",
    ".yml": "yaml",
}

PROJECT_MARKERS = {
    "rust": ["Cargo.toml"],
    "python": ["pyproject.toml", "setup.py", "requirements.txt"],
    "go": ["go.mod"],
    "typescript": ["tsconfig.json", "package.json"],
    "javascript": ["package.json"],
    "java": ["pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"],
    "kotlin": ["build.gradle.kts", "settings.gradle.kts", "pom.xml"],
    "bash": [".bashrc", ".bash_profile"],
    "yaml": ["docker-compose.yml", "docker-compose.yaml"],
}

TOOLCHAIN_INSTALLERS = {"npm", "pip", "cargo", "go"}
TOOLCHAIN_PACKAGE_RE = re.compile(r"^[A-Za-z0-9@._+:/=-]+$")
MAX_INSTALL_PACKAGES_PER_CALL = 10
INSTALL_AUDIT_FILE = WORKSPACE_ROOT / ".selu-coding" / "install_audit.jsonl"
TOOLCHAIN_ROOT = WORKSPACE_ROOT / ".selu-tools"

TOOLCHAIN_PACKAGE_ALLOWLIST = {
    "npm": {
        "typescript",
        "typescript-language-server",
        "bash-language-server",
        "yaml-language-server",
        "eslint",
        "prettier",
        "pnpm",
        "yarn",
        "@biomejs/biome",
    },
    "pip": {
        "pytest",
        "ruff",
        "mypy",
        "black",
        "pylint",
        "python-lsp-server",
        "poetry",
        "pipx",
    },
    "cargo": {
        "cargo-edit",
        "cargo-nextest",
        "cargo-watch",
        "cargo-audit",
        "cargo-deny",
        "cargo-outdated",
        "cargo-expand",
    },
    "go": {
        "golang.org/x/tools/gopls@latest",
        "honnef.co/go/tools/cmd/staticcheck@latest",
        "github.com/golangci/golangci-lint/cmd/golangci-lint@latest",
    },
}

CHECK_SPECS: List[Dict[str, Any]] = [
    {"command": "cargo test", "binaries": ["cargo"], "markers": ["Cargo.toml"]},
    {"command": "cargo check", "binaries": ["cargo"], "markers": ["Cargo.toml"]},
    {"command": "cargo fmt --check", "binaries": ["cargo"], "markers": ["Cargo.toml"]},
    {"command": "cargo clippy", "binaries": ["cargo"], "markers": ["Cargo.toml"]},
    {"command": "npm test", "binaries": ["npm"], "markers": ["package.json"]},
    {"command": "npm run test", "binaries": ["npm"], "markers": ["package.json"], "script": "test"},
    {"command": "npm run lint", "binaries": ["npm"], "markers": ["package.json"], "script": "lint"},
    {"command": "npm run build", "binaries": ["npm"], "markers": ["package.json"], "script": "build"},
    {"command": "pnpm test", "binaries": ["pnpm"], "markers": ["package.json"]},
    {"command": "pnpm lint", "binaries": ["pnpm"], "markers": ["package.json"]},
    {"command": "pnpm build", "binaries": ["pnpm"], "markers": ["package.json"]},
    {"command": "yarn test", "binaries": ["yarn"], "markers": ["package.json"]},
    {"command": "yarn lint", "binaries": ["yarn"], "markers": ["package.json"]},
    {"command": "yarn build", "binaries": ["yarn"], "markers": ["package.json"]},
    {"command": "pytest", "binaries": ["pytest"], "markers_any": ["pyproject.toml", "setup.py", "requirements.txt"]},
    {"command": "ruff check", "binaries": ["ruff"], "markers_any": ["pyproject.toml", "requirements.txt"]},
    {"command": "mypy", "binaries": ["mypy"], "markers_any": ["pyproject.toml", "mypy.ini"]},
    {"command": "go test", "binaries": ["go"], "markers": ["go.mod"]},
    {"command": "make test", "binaries": ["make"], "markers_any": ["Makefile", "makefile"]},
    {"command": "make lint", "binaries": ["make"], "markers_any": ["Makefile", "makefile"]},
    {"command": "make build", "binaries": ["make"], "markers_any": ["Makefile", "makefile"]},
]

state_lock = threading.Lock()
auth_cache_lock = threading.Lock()
installation_id_cache: Dict[str, int] = {}
installation_token_cache: Dict[int, Dict[str, Any]] = {}


class ToolError(Exception):
    pass


def _clip(text: str, max_chars: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def _first_non_empty(config: Dict[str, Any], keys: List[str]) -> str:
    for key in keys:
        value = str(config.get(key, "")).strip()
        if value:
            return value
    return ""


def _git_identity_from_config(config: Dict[str, Any]) -> Dict[str, str]:
    author_name = _first_non_empty(config, ["GIT_AUTHOR_NAME", "GITAUTHORNAME"])
    author_email = _first_non_empty(config, ["GIT_AUTHOR_EMAIL", "GITAUTHOREMAIL"])
    committer_name = _first_non_empty(config, ["GIT_COMMITTER_NAME", "GITCOMMITTERNAME"])
    committer_email = _first_non_empty(
        config,
        ["GIT_COMMITTER_EMAIL", "GITCOMMITTER_EMAIL", "GITCOMMITTEREMAIL"],
    )
    env: Dict[str, str] = {}
    if author_name:
        env["GIT_AUTHOR_NAME"] = author_name
    if author_email:
        env["GIT_AUTHOR_EMAIL"] = author_email
    if committer_name:
        env["GIT_COMMITTER_NAME"] = committer_name
    if committer_email:
        env["GIT_COMMITTER_EMAIL"] = committer_email
    return env


def _args_shape(args: Any) -> str:
    if not isinstance(args, dict):
        return f"type={type(args).__name__}"
    keys = sorted(args.keys())
    parts = [f"keys={keys}"]
    if isinstance(args.get("path"), str):
        parts.append(f"path_len={len(args['path'])}")
    if isinstance(args.get("find"), str):
        parts.append(f"find_len={len(args['find'])}")
    if isinstance(args.get("replace"), str):
        parts.append(f"replace_len={len(args['replace'])}")
    if isinstance(args.get("content"), str):
        parts.append(f"content_len={len(args['content'])}")
    files = args.get("files")
    if isinstance(files, list):
        parts.append(f"files_count={len(files)}")
    return " ".join(parts)


def _normalize_thread_id(thread_id: str) -> str:
    value = (thread_id or "").strip()
    return value if value else DEFAULT_THREAD_ID


def _load_all_state_unlocked() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {"threads": {}}
    try:
        raw = STATE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("threads"), dict):
            return data
    except Exception:
        pass
    return {"threads": {}}


def _save_all_state_unlocked(data: Dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def get_thread_state(thread_id: str) -> Dict[str, Any]:
    tid = _normalize_thread_id(thread_id)
    with state_lock:
        data = _load_all_state_unlocked()
        state = data.get("threads", {}).get(tid, {})
        if isinstance(state, dict):
            return dict(state)
        return {}


def set_thread_state(thread_id: str, state: Dict[str, Any]) -> None:
    tid = _normalize_thread_id(thread_id)
    with state_lock:
        data = _load_all_state_unlocked()
        threads = data.setdefault("threads", {})
        threads[tid] = state
        _save_all_state_unlocked(data)


def _safe_workspace_path(relative: str) -> Path:
    rel = relative.strip()
    if not rel:
        raise ToolError("Directory must not be empty.")
    target = (WORKSPACE_ROOT / rel).resolve()
    if not target.is_relative_to(WORKSPACE_ROOT):
        raise ToolError("Directory must stay inside /workspace.")
    return target


def _safe_repo_path(repo_root: Path, relative: str) -> Path:
    rel = (relative or ".").strip()
    candidate = (repo_root / rel).resolve()
    if not candidate.is_relative_to(repo_root.resolve()):
        raise ToolError("Path must stay inside the repository.")
    return candidate


def _parse_repository(repository: str) -> tuple[str, str]:
    value = repository.strip()
    if REPO_SHORTHAND_RE.match(value):
        owner, repo = value.split("/", 1)
        return owner, repo

    m = REPO_URL_RE.match(value)
    if m:
        owner = m.group(1)
        repo = m.group(2)
        return owner, repo

    raise ToolError(
        "Repository must be 'owner/repo' or a GitHub HTTPS URL like https://github.com/owner/repo."
    )


def _require_app_id(config: Dict[str, Any]) -> str:
    app_id = str(config.get("GITHUB_APP_ID", "")).strip()
    if not app_id:
        raise ToolError("Missing required credential: GITHUB_APP_ID")
    return app_id


def _require_app_private_key(config: Dict[str, Any]) -> str:
    private_key = str(config.get("GITHUB_APP_PRIVATE_KEY", "")).strip()
    if private_key:
        # Support one-line env-style PEM values with escaped newlines.
        if "\\n" in private_key and "\n" not in private_key:
            private_key = private_key.replace("\\n", "\n")
        return private_key

    private_key_b64 = str(config.get("GITHUB_APP_PRIVATE_KEY_BASE64", "")).strip()
    if private_key_b64:
        try:
            decoded = base64.b64decode(private_key_b64).decode("utf-8")
        except Exception as exc:
            raise ToolError(f"Invalid GITHUB_APP_PRIVATE_KEY_BASE64: {exc}") from exc
        return decoded

    raise ToolError(
        "Missing required credential: GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_BASE64"
    )


def _build_app_jwt(app_id: str, private_key: str) -> str:
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (9 * 60),
        "iss": app_id,
    }
    token = jwt.encode(payload, private_key, algorithm="RS256")
    if isinstance(token, bytes):
        return token.decode("utf-8")
    return token


def _github_api_json(
    *,
    url: str,
    method: str,
    bearer_token: str,
    payload: Dict[str, Any] | None = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    data: bytes | None = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    req = Request(url=url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {bearer_token}")
    req.add_header("X-GitHub-Api-Version", GITHUB_API_VERSION)
    if payload is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")[:2000]
        raise ToolError(f"GitHub API error {exc.code}: {_clip(body_text, 1200)}")
    except URLError as exc:
        raise ToolError(f"Network error calling GitHub API: {exc}")

    if not raw.strip():
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(f"GitHub API returned invalid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ToolError("GitHub API returned unexpected response shape.")
    return decoded


def _parse_iso_utc(iso_value: str) -> int | None:
    value = (iso_value or "").strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(dt.astimezone(timezone.utc).timestamp())
    except Exception:
        return None


def _owner_repo_from_state(state: Dict[str, Any]) -> tuple[str, str]:
    owner = str(state.get("owner", "")).strip()
    repo = str(state.get("repo", "")).strip()
    if not owner or not repo:
        raise ToolError("Repository owner/repo is missing. Run open_repository again.")
    return owner, repo


def _repo_key(owner: str, repo: str) -> str:
    return f"{owner}/{repo}".lower()


def _installation_id_for_repo(owner: str, repo: str, app_jwt: str) -> int:
    key = _repo_key(owner, repo)
    with auth_cache_lock:
        cached = installation_id_cache.get(key)
    if cached:
        return cached

    url = f"https://api.github.com/repos/{owner}/{repo}/installation"
    data = _github_api_json(url=url, method="GET", bearer_token=app_jwt)
    installation_id = int(data.get("id", 0))
    if installation_id <= 0:
        raise ToolError(
            f"Could not resolve GitHub App installation for {owner}/{repo}. "
            "Ensure the app is installed for that repository."
        )

    with auth_cache_lock:
        installation_id_cache[key] = installation_id
    return installation_id


def _installation_access_token(config: Dict[str, Any], owner: str, repo: str) -> str:
    app_id = _require_app_id(config)
    private_key = _require_app_private_key(config)
    app_jwt = _build_app_jwt(app_id, private_key)
    installation_id = _installation_id_for_repo(owner, repo, app_jwt)
    now = int(time.time())

    with auth_cache_lock:
        cached = installation_token_cache.get(installation_id)
        if cached:
            token = str(cached.get("token", "")).strip()
            expires_at = int(cached.get("expires_at", 0))
            if token and expires_at - 60 > now:
                return token

    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    data = _github_api_json(url=url, method="POST", bearer_token=app_jwt, payload={})
    token = str(data.get("token", "")).strip()
    if not token:
        raise ToolError("GitHub App did not return an installation access token.")

    expires_at = _parse_iso_utc(str(data.get("expires_at", "")))
    if expires_at is None:
        expires_at = now + 300

    with auth_cache_lock:
        installation_token_cache[installation_id] = {
            "token": token,
            "expires_at": expires_at,
        }
    return token


def _run_command(
    args: List[str],
    cwd: Path | None = None,
    env: Dict[str, str] | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _run_git(
    repo_root: Path,
    git_args: List[str],
    token: str | None = None,
    timeout: int = 180,
    extra_env: Dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git"]
    if token:
        # GitHub git-over-https expects Basic auth with x-access-token username.
        basic = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
        cmd.extend(["-c", f"http.extraHeader=Authorization: Basic {basic}"])
    cmd.extend(git_args)

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra_env:
        env.update(extra_env)
    return _run_command(cmd, cwd=repo_root, env=env, timeout=timeout)


def _run_git_checked(
    repo_root: Path,
    git_args: List[str],
    token: str | None = None,
    timeout: int = 180,
    extra_env: Dict[str, str] | None = None,
) -> str:
    cp = _run_git(repo_root, git_args, token=token, timeout=timeout, extra_env=extra_env)
    if cp.returncode != 0:
        stderr = _clip((cp.stderr or "").strip())
        stdout = _clip((cp.stdout or "").strip())
        detail = stderr or stdout or "Git command failed."
        raise ToolError(detail)
    return cp.stdout.strip()


def _detect_default_branch(repo_root: Path, token: str | None) -> str:
    cp = _run_git(repo_root, ["symbolic-ref", "refs/remotes/origin/HEAD"], token=token)
    if cp.returncode == 0:
        ref = cp.stdout.strip()
        if ref.startswith("refs/remotes/origin/"):
            return ref.replace("refs/remotes/origin/", "", 1)

    cp = _run_git(repo_root, ["remote", "show", "origin"], token=token)
    if cp.returncode == 0:
        for line in cp.stdout.splitlines():
            line = line.strip()
            if line.lower().startswith("head branch:"):
                return line.split(":", 1)[1].strip()

    return "main"


def _current_branch(repo_root: Path) -> str:
    return _run_git_checked(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"])


def _local_branch_exists(repo_root: Path, branch: str) -> bool:
    cp = _run_git(repo_root, ["show-ref", "--verify", f"refs/heads/{branch}"])
    return cp.returncode == 0


def _remote_branch_exists(repo_root: Path, branch: str, token: str | None = None) -> bool:
    cp = _run_git(
        repo_root,
        ["ls-remote", "--heads", "origin", branch],
        token=token,
        timeout=120,
    )
    if cp.returncode != 0:
        return False
    return bool((cp.stdout or "").strip())


def _commit_count_between(repo_root: Path, base: str, head: str) -> int:
    out = _run_git_checked(repo_root, ["rev-list", "--count", f"{base}..{head}"])
    try:
        return int(out.strip())
    except Exception:
        raise ToolError(f"Failed to parse commit distance for {base}..{head}.")


def _ensure_local_branch(repo_root: Path, branch: str) -> None:
    check = _run_git(repo_root, ["show-ref", "--verify", f"refs/heads/{branch}"])
    if check.returncode == 0:
        _run_git_checked(repo_root, ["checkout", branch])
    else:
        _run_git_checked(repo_root, ["checkout", "-B", branch, f"origin/{branch}"])


def _repo_state_or_error(thread_id: str) -> Dict[str, Any]:
    state = get_thread_state(thread_id)
    repo_path = state.get("repo_path")
    if not repo_path:
        raise ToolError("No repository is active. Run open_repository first.")
    root = Path(str(repo_path)).resolve()
    if not root.exists() or not (root / ".git").exists():
        raise ToolError("Stored repository path is missing. Run open_repository again.")
    if not root.is_relative_to(WORKSPACE_ROOT):
        raise ToolError("Repository path must stay inside /workspace.")
    state["repo_root"] = str(root)
    return state


def _slugify_feature(value: str) -> str:
    raw = value.strip().lower()
    if raw.startswith("feature-"):
        raw = raw[len("feature-") :]
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    if not slug:
        raise ToolError("Feature name must contain letters or numbers.")
    return f"feature-{slug}"


def _is_allowed_check_command(cmd: str) -> bool:
    if not cmd or "\n" in cmd or "\r" in cmd:
        return False

    banned = ["&&", "||", "|", ";", "`", "$(", ">", "<"]
    if any(token in cmd for token in banned):
        return False

    return True


def _validate_toolchain_packages(manager: str, packages: Any) -> List[str]:
    if not isinstance(packages, list) or not packages:
        raise ToolError("install_toolchain requires a non-empty packages array.")
    if len(packages) > MAX_INSTALL_PACKAGES_PER_CALL:
        raise ToolError(f"At most {MAX_INSTALL_PACKAGES_PER_CALL} packages are allowed per install call.")
    cleaned: List[str] = []
    allowlist = TOOLCHAIN_PACKAGE_ALLOWLIST.get(manager, set())
    for raw in packages:
        if not isinstance(raw, str):
            raise ToolError("Each package must be a string.")
        pkg = raw.strip()
        if not pkg:
            raise ToolError("Package names must not be empty.")
        if not TOOLCHAIN_PACKAGE_RE.match(pkg):
            raise ToolError(f"Invalid package name: {pkg}")
        if pkg not in allowlist:
            raise ToolError(f"Package is not allowlisted for {manager}: {pkg}")
        cleaned.append(pkg)
    return cleaned


def _command_exists(binary: str) -> bool:
    return _run_command(["which", binary]).returncode == 0


def _lsp_log(event: str, thread_id: str, **fields: Any) -> None:
    rendered = " ".join(f"{k}={v}" for k, v in fields.items())
    log.info("LSP event=%s thread_id=%s %s", event, thread_id, rendered)


def _append_install_audit(entry: Dict[str, Any]) -> None:
    INSTALL_AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with INSTALL_AUDIT_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _bump_metric(state: Dict[str, Any], key: str, amount: int = 1) -> None:
    metrics = state.setdefault("metrics", {})
    value = int(metrics.get(key, 0))
    metrics[key] = value + amount


def _record_check_metric(state: Dict[str, Any], command: str, exit_code: int) -> None:
    metrics = state.setdefault("metrics", {})
    checks = metrics.setdefault("checks", {})
    item = checks.setdefault(command, {"pass": 0, "fail": 0})
    if exit_code == 0:
        item["pass"] = int(item.get("pass", 0)) + 1
    else:
        item["fail"] = int(item.get("fail", 0)) + 1


def _read_package_json(repo_root: Path) -> Dict[str, Any]:
    target = repo_root / "package.json"
    if not target.exists() or not target.is_file():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except Exception:
        return {}
    return {}


def _check_spec_reason(repo_root: Path, spec: Dict[str, Any], package_json: Dict[str, Any]) -> str | None:
    missing_bins = [b for b in spec.get("binaries", []) if not _command_exists(str(b))]
    if missing_bins:
        return f"missing binaries: {', '.join(missing_bins)}"

    markers = [str(m) for m in spec.get("markers", [])]
    if markers and any(not (repo_root / marker).exists() for marker in markers):
        return f"missing required markers: {', '.join(markers)}"

    markers_any = [str(m) for m in spec.get("markers_any", [])]
    if markers_any and not any((repo_root / marker).exists() for marker in markers_any):
        return f"missing any of markers: {', '.join(markers_any)}"

    script = str(spec.get("script", "")).strip()
    if script:
        scripts = package_json.get("scripts")
        if not isinstance(scripts, dict) or script not in scripts:
            return f"package.json missing script: {script}"

    return None


def _available_checks_with_reasons(repo_root: Path) -> Dict[str, Any]:
    package_json = _read_package_json(repo_root)
    available: List[str] = []
    unavailable: List[Dict[str, str]] = []
    for spec in CHECK_SPECS:
        command = str(spec["command"])
        reason = _check_spec_reason(repo_root, spec, package_json)
        if reason is None:
            available.append(command)
        else:
            unavailable.append({"command": command, "reason": reason})
    return {"available": available, "unavailable": unavailable}


def _probe_binary(binary: str) -> Dict[str, Any]:
    cp = _run_command(["which", binary])
    found = cp.returncode == 0 and bool((cp.stdout or "").strip())
    return {
        "binary": binary,
        "found": found,
        "path": (cp.stdout or "").strip() if found else None,
    }


def _path_to_uri(path: Path) -> str:
    return path.resolve().as_uri()


def _uri_to_path(uri: str) -> str:
    if not uri.startswith("file://"):
        return uri
    path = Path(uri[7:])
    return path.as_posix()


def _detect_project_language(repo_root: Path) -> str | None:
    for language, markers in PROJECT_MARKERS.items():
        for marker in markers:
            if (repo_root / marker).exists():
                return language
    return None


def _infer_language_from_path(path: str) -> str | None:
    ext = Path(path).suffix.lower()
    return LANGUAGE_IDS_BY_EXTENSION.get(ext)


def _find_lsp_server_command(language: str | None) -> List[str] | None:
    if not language:
        return None
    command = LSP_SERVER_BY_LANGUAGE.get(language)
    if not command:
        return None
    bin_name = command[0]
    if _run_command(["which", bin_name]).returncode != 0:
        return None
    return command


class _LspClient:
    def __init__(self, command: List[str], cwd: Path):
        self._next_id = 1
        self._proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if self._proc.stdin is None or self._proc.stdout is None:
            raise ToolError("Failed to start LSP server.")
        self._stdin = self._proc.stdin
        self._stdout = self._proc.stdout

    def close(self) -> None:
        try:
            self.notify("exit", {})
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=2)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass

    def _write_message(self, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self._stdin.write(header + body)
        self._stdin.flush()

    def _read_exact(self, n: int, deadline: float) -> bytes:
        buf = b""
        while len(buf) < n:
            remaining = max(0.0, deadline - time.time())
            if remaining <= 0:
                raise ToolError("LSP response timed out.")
            readable, _, _ = select.select([self._stdout], [], [], remaining)
            if not readable:
                raise ToolError("LSP response timed out.")
            chunk = self._stdout.read(n - len(buf))
            if not chunk:
                raise ToolError("LSP server closed the stream.")
            buf += chunk
        return buf

    def _read_message(self, deadline: float) -> Dict[str, Any]:
        header_bytes = b""
        while b"\r\n\r\n" not in header_bytes:
            remaining = max(0.0, deadline - time.time())
            if remaining <= 0:
                raise ToolError("LSP response timed out.")
            readable, _, _ = select.select([self._stdout], [], [], remaining)
            if not readable:
                raise ToolError("LSP response timed out.")
            chunk = self._stdout.read(1)
            if not chunk:
                raise ToolError("LSP server closed before header.")
            header_bytes += chunk
        header_text, _ = header_bytes.split(b"\r\n\r\n", 1)
        headers = header_text.decode("ascii", errors="replace").split("\r\n")
        content_length = 0
        for line in headers:
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())
        if content_length <= 0:
            raise ToolError("Invalid LSP response header.")
        body = self._read_exact(content_length, deadline)
        data = json.loads(body.decode("utf-8", errors="replace"))
        if not isinstance(data, dict):
            raise ToolError("Invalid LSP response payload.")
        return data

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self._write_message(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            }
        )

    def request(self, method: str, params: Dict[str, Any], timeout_seconds: int = 20) -> Any:
        req_id = self._next_id
        self._next_id += 1
        self._write_message(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            }
        )

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            msg = self._read_message(deadline)
            if "id" not in msg:
                continue
            if msg.get("id") != req_id:
                continue
            if "error" in msg and msg["error"]:
                err = msg["error"]
                if isinstance(err, dict):
                    raise ToolError(f"LSP request failed: {err.get('message', 'unknown error')}")
                raise ToolError("LSP request failed.")
            return msg.get("result")
        raise ToolError(f"LSP request timed out: {method}")

    def initialize(self, root_path: Path) -> None:
        self.request(
            "initialize",
            {
                "processId": None,
                "rootUri": _path_to_uri(root_path),
                "capabilities": {},
                "workspaceFolders": [
                    {
                        "name": root_path.name,
                        "uri": _path_to_uri(root_path),
                    }
                ],
            },
            timeout_seconds=30,
        )
        self.notify("initialized", {})


def _lsp_client_for_repo(repo_root: Path, language: str | None) -> tuple[_LspClient, List[str], str]:
    lang = language or _detect_project_language(repo_root)
    command = _find_lsp_server_command(lang)
    if not command:
        detected = lang or "unknown"
        raise ToolError(
            f"No supported LSP server found for language '{detected}'. Install one of: rust-analyzer, pylsp, gopls, typescript-language-server, jdtls, kotlin-language-server, bash-language-server, yaml-language-server."
        )
    client = _LspClient(command, cwd=repo_root)
    client.initialize(repo_root)
    return client, command, lang or "unknown"


def _did_open_document(client: _LspClient, file_path: Path, language_id: str, text: str) -> None:
    client.notify(
        "textDocument/didOpen",
        {
            "textDocument": {
                "uri": _path_to_uri(file_path),
                "languageId": language_id,
                "version": 1,
                "text": text,
            }
        },
    )


def handle_open_repository(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    repository = str(args.get("repository", "")).strip()
    if not repository:
        raise ToolError("open_repository requires 'repository'.")

    owner, repo = _parse_repository(repository)
    token = _installation_access_token(config, owner, repo)

    directory = str(args.get("directory", "")).strip()
    if directory:
        repo_root = _safe_workspace_path(directory)
    else:
        repo_root = _safe_workspace_path(f"repos/{owner}__{repo}")

    previous_branch = ""
    if repo_root.exists():
        if not (repo_root / ".git").exists():
            raise ToolError("Target directory exists but is not a git repository.")
        try:
            previous_branch = _current_branch(repo_root)
        except Exception:
            previous_branch = ""
        _run_git_checked(repo_root, ["fetch", "--all", "--prune"], token=token)
    else:
        repo_root.parent.mkdir(parents=True, exist_ok=True)
        clone_url = f"https://github.com/{owner}/{repo}.git"
        _run_git_checked(WORKSPACE_ROOT, ["clone", clone_url, str(repo_root)], token=token, timeout=600)

    base_branch = _detect_default_branch(repo_root, token)
    _ensure_local_branch(repo_root, base_branch)
    _run_git(repo_root, ["pull", "--ff-only", "origin", base_branch], token=token, timeout=240)

    # Preserve the previously active non-base branch across continuation turns.
    if previous_branch and previous_branch != base_branch and _local_branch_exists(repo_root, previous_branch):
        _run_git_checked(repo_root, ["checkout", previous_branch])

    current_branch = _current_branch(repo_root)
    status = _run_git_checked(repo_root, ["status", "--short", "--branch"])

    state = get_thread_state(thread_id)
    state.update(
        {
            "repository": repository,
            "owner": owner,
            "repo": repo,
            "repo_path": str(repo_root),
            "base_branch": base_branch,
            "current_branch": current_branch,
            "last_checks": None,
        }
    )
    set_thread_state(thread_id, state)

    return {
        "ok": True,
        "owner": owner,
        "repo": repo,
        "repo_path": str(repo_root),
        "base_branch": base_branch,
        "current_branch": current_branch,
        "status": _clip(status),
    }


def handle_create_feature_branch(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    feature = str(args.get("feature", "")).strip()
    if not feature:
        raise ToolError("create_feature_branch requires 'feature'.")

    state = _repo_state_or_error(thread_id)
    repo_root = Path(state["repo_root"])
    owner, repo = _owner_repo_from_state(state)
    token = _installation_access_token(config, owner, repo)

    branch_name = _slugify_feature(feature)
    from_base = bool(args.get("from_base", False))
    force_reset = bool(args.get("force_reset", False))

    branch_exists_local = _local_branch_exists(repo_root, branch_name)
    branch_exists_remote = _remote_branch_exists(repo_root, branch_name, token=token)

    if (branch_exists_local or branch_exists_remote) and not force_reset:
        if not branch_exists_local and branch_exists_remote:
            _run_git_checked(repo_root, ["checkout", "-B", branch_name, f"origin/{branch_name}"])
        else:
            _run_git_checked(repo_root, ["checkout", branch_name])
        reused_existing = True
    elif from_base:
        base = str(state.get("base_branch") or _detect_default_branch(repo_root, token))
        _ensure_local_branch(repo_root, base)
        _run_git(repo_root, ["pull", "--ff-only", "origin", base], token=token, timeout=240)
        _run_git_checked(repo_root, ["checkout", "-B", branch_name, base])
        reused_existing = False
    else:
        _run_git_checked(repo_root, ["checkout", "-B", branch_name])
        reused_existing = False

    state["current_branch"] = branch_name
    set_thread_state(thread_id, state)

    return {
        "ok": True,
        "branch": branch_name,
        "base_branch": state.get("base_branch"),
        "reused_existing_branch": reused_existing,
        "from_base": from_base,
        "force_reset": force_reset,
    }


def handle_list_files(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    del config
    state = _repo_state_or_error(thread_id)
    repo_root = Path(state["repo_root"])

    rel_path = str(args.get("path", "."))
    max_entries = int(args.get("max_entries", 200))
    max_entries = max(1, min(max_entries, 2000))

    target = _safe_repo_path(repo_root, rel_path)
    if target.is_file():
        rel = target.relative_to(repo_root).as_posix()
        return {"ok": True, "files": [rel], "truncated": False}

    if not target.exists():
        raise ToolError("Path does not exist.")

    target_rel = target.relative_to(repo_root)
    cp = _run_command(["rg", "--files", str(target_rel)], cwd=repo_root, timeout=120)

    files: List[str] = []
    if cp.returncode == 0:
        files = [line.strip() for line in cp.stdout.splitlines() if line.strip()]
    else:
        for root, _, names in os.walk(target):
            root_path = Path(root)
            for name in names:
                files.append((root_path / name).relative_to(repo_root).as_posix())

    files.sort()
    truncated = len(files) > max_entries
    return {
        "ok": True,
        "files": files[:max_entries],
        "total": len(files),
        "truncated": truncated,
    }


def handle_read_file(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    del config
    state = _repo_state_or_error(thread_id)
    repo_root = Path(state["repo_root"])

    rel_path = str(args.get("path", "")).strip()
    if not rel_path:
        raise ToolError("read_file requires 'path'.")

    max_bytes = int(args.get("max_bytes", 20000))
    max_bytes = max(256, min(max_bytes, 200000))

    target = _safe_repo_path(repo_root, rel_path)
    if not target.exists() or not target.is_file():
        raise ToolError("File not found.")

    raw = target.read_bytes()
    sliced = raw[:max_bytes]
    return {
        "ok": True,
        "path": target.relative_to(repo_root).as_posix(),
        "content": sliced.decode("utf-8", errors="replace"),
        "size_bytes": len(raw),
        "truncated": len(raw) > max_bytes,
    }


def handle_search_text(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    del config
    state = _repo_state_or_error(thread_id)
    repo_root = Path(state["repo_root"])

    query = str(args.get("query", "")).strip()
    if not query:
        raise ToolError("search_text requires 'query'.")

    rel_path = str(args.get("path", "."))
    max_results = int(args.get("max_results", 100))
    max_results = max(1, min(max_results, 1000))

    target = _safe_repo_path(repo_root, rel_path)
    if not target.exists():
        raise ToolError("Search path does not exist.")

    target_rel = target.relative_to(repo_root)
    cp = _run_command(
        [
            "rg",
            "-n",
            "-H",
            "--no-heading",
            "--color",
            "never",
            "-e",
            query,
            str(target_rel),
        ],
        cwd=repo_root,
        timeout=120,
    )

    if cp.returncode not in (0, 1):
        raise ToolError(_clip((cp.stderr or cp.stdout or "Search failed").strip()))

    matches = []
    for line in cp.stdout.splitlines():
        if len(matches) >= max_results:
            break
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        file_path, line_no, text = parts
        matches.append(
            {
                "path": file_path,
                "line": int(line_no) if line_no.isdigit() else line_no,
                "text": _clip(text, 600),
            }
        )

    _bump_metric(state, "search_calls")
    lsp_status = str(state.get("lsp_status", "")).strip().lower()
    if lsp_status in {"unavailable", "failure"}:
        _bump_metric(state, "search_fallback_calls")
    set_thread_state(thread_id, state)

    return {
        "ok": True,
        "query": query,
        "matches": matches,
        "count": len(matches),
        "truncated": len(cp.stdout.splitlines()) > max_results,
    }


def handle_apply_patch(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    del config
    state = _repo_state_or_error(thread_id)
    repo_root = Path(state["repo_root"])

    updates: List[Dict[str, Any]] = []
    mode = "unknown"
    batch = args.get("files")
    if isinstance(batch, list) and batch:
        updates = [item for item in batch if isinstance(item, dict)]
        mode = "batch_full_content"
    else:
        path = args.get("path")
        content = args.get("content")
        if isinstance(path, str) and isinstance(content, str):
            updates = [{"path": path, "content": content}]
            mode = "single_full_content"
        else:
            find = args.get("find")
            replace = args.get("replace")
            if isinstance(path, str) and isinstance(find, str) and isinstance(replace, str):
                updates = [{"path": path, "find": find, "replace": replace}]
                mode = "single_find_replace"

    if not updates:
        raise ToolError(
            "Invalid apply_patch args. Send either "
            '{"path":"repo/file","content":"<full file text>"} '
            "or "
            '{"files":[{"path":"repo/file","content":"<full file text>"}]} '
            "or "
            '{"path":"repo/file","find":"<exact old text>","replace":"<new text>"}. '
            "Prefer write_file/replace_in_file/write_files for new calls. "
            f"Received {_args_shape(args)}."
        )

    changed = []
    for update in updates:
        rel_path = str(update.get("path", "")).strip()
        target = _safe_repo_path(repo_root, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        old = target.read_text(encoding="utf-8") if target.exists() else ""

        content = update.get("content")
        if isinstance(content, str):
            if not rel_path:
                raise ToolError("Each file update needs a non-empty string path.")

            if old == content:
                continue

            target.write_text(content, encoding="utf-8")
            changed.append(target.relative_to(repo_root).as_posix())
            continue

        find = update.get("find")
        replace = update.get("replace")
        if not rel_path or not isinstance(find, str) or not isinstance(replace, str):
            raise ToolError("Each file update needs either path+content or path+find+replace.")
        if not target.exists() or not target.is_file():
            raise ToolError(f"File does not exist for find/replace: {rel_path}")
        if not find:
            raise ToolError("find must be a non-empty string for find/replace mode.")
        count = old.count(find)
        if count == 0:
            raise ToolError(f"find text was not found in file: {rel_path}")
        if count > 1:
            raise ToolError(
                f"find text matches {count} locations in {rel_path}; provide a more specific snippet."
            )
        new_content = old.replace(find, replace, 1)
        if new_content == old:
            continue

        target.write_text(new_content, encoding="utf-8")
        changed.append(target.relative_to(repo_root).as_posix())

    log.info("apply_patch completed mode=%s changed_count=%d", mode, len(changed))
    return {
        "ok": True,
        "changed_files": changed,
        "changed_count": len(changed),
        "mode": mode,
    }


def handle_write_file(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    del config
    path = args.get("path")
    content = args.get("content")
    if not isinstance(path, str) or not isinstance(content, str) or not path.strip():
        raise ToolError(
            "write_file requires string path and content. "
            'Example: {"path":"src/main.py","content":"<full file text>"}. '
            f"Received {_args_shape(args)}."
        )
    return handle_apply_patch({"path": path, "content": content}, {}, thread_id)


def handle_replace_in_file(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    del config
    state = _repo_state_or_error(thread_id)
    repo_root = Path(state["repo_root"])

    path = args.get("path")
    find = args.get("find")
    replace = args.get("replace")
    replace_all = bool(args.get("replace_all", False))

    if not isinstance(path, str) or not path.strip():
        raise ToolError("replace_in_file requires a non-empty string path.")
    if not isinstance(find, str) or not find:
        raise ToolError("replace_in_file requires a non-empty string find.")
    if not isinstance(replace, str):
        raise ToolError("replace_in_file requires string replace.")

    target = _safe_repo_path(repo_root, path)
    if not target.exists() or not target.is_file():
        raise ToolError(f"File does not exist: {path}")

    old = target.read_text(encoding="utf-8")
    count = old.count(find)
    if count == 0:
        raise ToolError(f"find text was not found in file: {path}")
    if count > 1 and not replace_all:
        raise ToolError(
            f"find text matches {count} locations in {path}; set replace_all=true or provide a more specific snippet."
        )

    if replace_all:
        new_content = old.replace(find, replace)
        replaced_count = count
    else:
        new_content = old.replace(find, replace, 1)
        replaced_count = 1

    if new_content == old:
        changed_files: List[str] = []
    else:
        target.write_text(new_content, encoding="utf-8")
        changed_files = [target.relative_to(repo_root).as_posix()]

    return {
        "ok": True,
        "changed_files": changed_files,
        "changed_count": len(changed_files),
        "mode": "replace_in_file",
        "matches_found": count,
        "replaced_count": replaced_count,
        "replace_all": replace_all,
    }


def handle_write_files(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    del config
    files = args.get("files")
    if not isinstance(files, list) or not files:
        raise ToolError(
            "write_files requires a non-empty files array. "
            'Example: {"files":[{"path":"src/main.py","content":"<full file text>"}]}. '
            f"Received {_args_shape(args)}."
        )
    return handle_apply_patch({"files": files}, {}, thread_id)


def handle_run_checks(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    del config
    state = _repo_state_or_error(thread_id)
    repo_root = Path(state["repo_root"])

    commands = args.get("commands")
    available_info = _available_checks_with_reasons(repo_root)
    available_commands = set(available_info["available"])
    if commands is None:
        commands = sorted(available_commands)
    if not isinstance(commands, list) or not commands:
        raise ToolError(
            "No runnable checks available. Install missing toolchain pieces or pass explicit commands."
        )

    stop_on_failure = bool(args.get("stop_on_failure", True))

    results = []
    for raw in commands:
        if not isinstance(raw, str):
            raise ToolError("Each command must be a string.")
        cmd = raw.strip()
        if not _is_allowed_check_command(cmd):
            raise ToolError(f"Command is invalid or contains forbidden shell operators: {cmd}")
        if cmd not in available_commands:
            reason = "command is not runnable in this repository/toolchain"
            for item in available_info["unavailable"]:
                if item.get("command") == cmd:
                    reason = str(item.get("reason") or reason)
                    break
            raise ToolError(f"Command is not currently available: {cmd} ({reason})")

        start = time.time()
        cp = _run_command(shlex.split(cmd), cwd=repo_root, timeout=1800)
        duration = round(time.time() - start, 3)

        result = {
            "command": cmd,
            "exit_code": cp.returncode,
            "duration_seconds": duration,
            "stdout": _clip(cp.stdout or ""),
            "stderr": _clip(cp.stderr or ""),
        }
        results.append(result)
        _record_check_metric(state, cmd, cp.returncode)

        if cp.returncode != 0 and stop_on_failure:
            break

    all_passed = all(r["exit_code"] == 0 for r in results)

    state["last_checks"] = {
        "all_passed": all_passed,
        "results": [{"command": r["command"], "exit_code": r["exit_code"]} for r in results],
        "ran_at": int(time.time()),
    }
    set_thread_state(thread_id, state)

    return {
        "ok": True,
        "all_passed": all_passed,
        "results": results,
        "available_commands": sorted(available_commands),
        "unavailable_commands": available_info["unavailable"],
    }


def handle_install_toolchain(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    del config
    state = get_thread_state(thread_id)
    repo_path = str(state.get("repo_path", "")).strip()
    repo_root = Path(repo_path).resolve() if repo_path else WORKSPACE_ROOT
    if repo_path and (not repo_root.exists() or not repo_root.is_relative_to(WORKSPACE_ROOT)):
        repo_root = WORKSPACE_ROOT

    manager = str(args.get("manager", "")).strip().lower()
    if not manager:
        raise ToolError("install_toolchain requires 'manager'.")
    if manager not in TOOLCHAIN_INSTALLERS:
        raise ToolError(
            f"Unsupported manager: {manager}. Supported: {', '.join(sorted(TOOLCHAIN_INSTALLERS))}."
        )

    packages = _validate_toolchain_packages(manager, args.get("packages"))
    global_install = bool(args.get("global", True))

    commands: List[List[str]] = []
    env = os.environ.copy()
    if manager == "npm":
        npm_prefix = TOOLCHAIN_ROOT / "npm"
        npm_prefix.mkdir(parents=True, exist_ok=True)
        npm_args = ["npm", "install", "--prefix", str(npm_prefix)]
        commands.append(npm_args + packages)
    elif manager == "pip":
        pip_target = TOOLCHAIN_ROOT / "pip"
        pip_target.mkdir(parents=True, exist_ok=True)
        pip_args = ["python3", "-m", "pip", "install", "--disable-pip-version-check", "--target", str(pip_target)]
        commands.append(pip_args + packages)
    elif manager == "cargo":
        if not _command_exists("cargo"):
            raise ToolError("cargo is not installed. Install Rust toolchain first.")
        cargo_root = TOOLCHAIN_ROOT / "cargo"
        cargo_root.mkdir(parents=True, exist_ok=True)
        commands.append(["cargo", "install", "--root", str(cargo_root), *packages])
    elif manager == "go":
        if not _command_exists("go"):
            raise ToolError("go is not installed.")
        go_bin = TOOLCHAIN_ROOT / "go" / "bin"
        go_bin.mkdir(parents=True, exist_ok=True)
        env["GOBIN"] = str(go_bin)
        commands.extend([["go", "install", pkg] for pkg in packages])

    results = []
    for cmd in commands:
        start = time.time()
        cp = _run_command(cmd, cwd=repo_root, env=env, timeout=1800)
        duration = round(time.time() - start, 3)
        results.append(
            {
                "command": " ".join(cmd),
                "exit_code": cp.returncode,
                "duration_seconds": duration,
                "stdout": _clip(cp.stdout or ""),
                "stderr": _clip(cp.stderr or ""),
            }
        )
        if cp.returncode != 0:
            break

    success = all(item["exit_code"] == 0 for item in results)
    audit = {
        "timestamp": int(time.time()),
        "thread_id": thread_id,
        "manager": manager,
        "packages": packages,
        "global": global_install,
        "success": success,
        "commands": [r["command"] for r in results],
    }
    _append_install_audit(audit)
    return {
        "ok": success,
        "manager": manager,
        "global": global_install,
        "packages": packages,
        "results": results,
        "installed_count": len(packages) if success else 0,
        "audit_file": str(INSTALL_AUDIT_FILE),
        "toolchain_root": str(TOOLCHAIN_ROOT),
    }


def handle_list_checks(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    del args, config
    state = _repo_state_or_error(thread_id)
    repo_root = Path(state["repo_root"])
    info = _available_checks_with_reasons(repo_root)
    return {
        "ok": True,
        "available": info["available"],
        "unavailable": info["unavailable"],
        "count_available": len(info["available"]),
    }


def handle_metrics_report(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    del args, config
    state = get_thread_state(thread_id)
    metrics = state.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    return {
        "ok": True,
        "thread_id": thread_id,
        "metrics": metrics,
    }


def handle_toolchain_probe(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    del args, config
    state = get_thread_state(thread_id)
    repo_path = str(state.get("repo_path", "")).strip()
    repo_root = Path(repo_path).resolve() if repo_path else None

    toolchain_dirs = {
        "root": str(TOOLCHAIN_ROOT),
        "npm": str(TOOLCHAIN_ROOT / "npm"),
        "pip": str(TOOLCHAIN_ROOT / "pip"),
        "cargo": str(TOOLCHAIN_ROOT / "cargo"),
        "go_bin": str(TOOLCHAIN_ROOT / "go" / "bin"),
    }

    binaries = [
        "git",
        "rg",
        "python3",
        "pip",
        "npm",
        "cargo",
        "go",
        "rust-analyzer",
        "gopls",
        "typescript-language-server",
        "bash-language-server",
        "yaml-language-server",
    ]
    binary_status = [_probe_binary(name) for name in binaries]
    lsp_status = [_probe_binary(cmd[0]) for cmd in LSP_SERVER_BY_LANGUAGE.values()]

    available_checks: Dict[str, Any] | None = None
    if repo_root and repo_root.exists() and repo_root.is_relative_to(WORKSPACE_ROOT):
        available_checks = _available_checks_with_reasons(repo_root)

    path_hints = {
        "prepend_path": [
            str(TOOLCHAIN_ROOT / "npm" / "node_modules" / ".bin"),
            str(TOOLCHAIN_ROOT / "cargo" / "bin"),
            str(TOOLCHAIN_ROOT / "go" / "bin"),
        ],
        "pythonpath": str(TOOLCHAIN_ROOT / "pip"),
    }

    return {
        "ok": True,
        "thread_id": thread_id,
        "repo_path": str(repo_root) if repo_root else None,
        "install_audit_file": str(INSTALL_AUDIT_FILE),
        "supported_install_managers": sorted(TOOLCHAIN_INSTALLERS),
        "package_allowlist": {k: sorted(v) for k, v in TOOLCHAIN_PACKAGE_ALLOWLIST.items()},
        "toolchain_dirs": toolchain_dirs,
        "toolchain_dirs_exist": {k: Path(v).exists() for k, v in toolchain_dirs.items()},
        "binaries": binary_status,
        "lsp_servers": lsp_status,
        "checks": available_checks,
        "path_hints": path_hints,
    }


def handle_lsp_probe(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    del args, config
    state = _repo_state_or_error(thread_id)
    repo_root = Path(state["repo_root"])

    project_language = _detect_project_language(repo_root)
    command = _find_lsp_server_command(project_language)
    available_servers = {}
    for language, cmd in LSP_SERVER_BY_LANGUAGE.items():
        available_servers[language] = _run_command(["which", cmd[0]]).returncode == 0

    _bump_metric(state, "lsp_probe_calls")
    state["lsp_status"] = "available" if command else "unavailable"
    if command:
        _bump_metric(state, "lsp_probe_success")
    else:
        _bump_metric(state, "lsp_probe_unavailable")
    set_thread_state(thread_id, state)

    _lsp_log(
        "probe",
        thread_id,
        project_language=project_language or "unknown",
        detected_server=command[0] if command else "none",
        available=sum(1 for v in available_servers.values() if v),
    )

    return {
        "ok": True,
        "project_language": project_language,
        "detected_server": command[0] if command else None,
        "available_servers": available_servers,
    }


def handle_lsp_definition(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    del config
    state = _repo_state_or_error(thread_id)
    repo_root = Path(state["repo_root"])

    rel_path = str(args.get("path", "")).strip()
    if not rel_path:
        raise ToolError("lsp_definition requires 'path'.")
    line = int(args.get("line", -1))
    character = int(args.get("character", -1))
    if line < 0 or character < 0:
        raise ToolError("lsp_definition requires non-negative 'line' and 'character'.")

    target = _safe_repo_path(repo_root, rel_path)
    if not target.exists() or not target.is_file():
        raise ToolError("File not found.")
    content = target.read_text(encoding="utf-8", errors="replace")

    language = str(args.get("language", "")).strip() or _infer_language_from_path(rel_path)
    language_id = language or "plaintext"
    _lsp_log(
        "definition_attempt",
        thread_id,
        path=rel_path,
        line=line,
        character=character,
        language=language or "auto",
    )
    _bump_metric(state, "lsp_definition_calls")
    set_thread_state(thread_id, state)
    client, command, resolved_language = _lsp_client_for_repo(repo_root, language)
    try:
        _did_open_document(client, target, language_id, content)
        result = client.request(
            "textDocument/definition",
            {
                "textDocument": {"uri": _path_to_uri(target)},
                "position": {"line": line, "character": character},
            },
        )
    finally:
        client.close()

    locations = []
    if isinstance(result, dict):
        result = [result]
    if isinstance(result, list):
        for item in result[:50]:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri") or item.get("targetUri")
            rng = item.get("range", {}) or item.get("targetSelectionRange", {})
            start = rng.get("start", {})
            locations.append(
                {
                    "path": _uri_to_path(str(uri)),
                    "line": int(start.get("line", 0)),
                    "character": int(start.get("character", 0)),
                }
            )

    _lsp_log(
        "definition_success",
        thread_id,
        path=rel_path,
        language=resolved_language,
        server=command[0],
        count=len(locations),
    )
    state["lsp_status"] = "available"
    _bump_metric(state, "lsp_definition_success")
    set_thread_state(thread_id, state)

    return {
        "ok": True,
        "language": resolved_language,
        "server_command": command,
        "locations": locations,
        "count": len(locations),
    }


def handle_lsp_references(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    del config
    state = _repo_state_or_error(thread_id)
    repo_root = Path(state["repo_root"])

    rel_path = str(args.get("path", "")).strip()
    if not rel_path:
        raise ToolError("lsp_references requires 'path'.")
    line = int(args.get("line", -1))
    character = int(args.get("character", -1))
    if line < 0 or character < 0:
        raise ToolError("lsp_references requires non-negative 'line' and 'character'.")

    target = _safe_repo_path(repo_root, rel_path)
    if not target.exists() or not target.is_file():
        raise ToolError("File not found.")
    content = target.read_text(encoding="utf-8", errors="replace")

    language = str(args.get("language", "")).strip() or _infer_language_from_path(rel_path)
    language_id = language or "plaintext"
    include_declaration = bool(args.get("include_declaration", False))
    max_results = int(args.get("max_results", 200))
    max_results = max(1, min(max_results, 1000))

    _lsp_log(
        "references_attempt",
        thread_id,
        path=rel_path,
        line=line,
        character=character,
        language=language or "auto",
        include_declaration=include_declaration,
        max_results=max_results,
    )
    _bump_metric(state, "lsp_references_calls")
    set_thread_state(thread_id, state)

    client, command, resolved_language = _lsp_client_for_repo(repo_root, language)
    try:
        _did_open_document(client, target, language_id, content)
        result = client.request(
            "textDocument/references",
            {
                "textDocument": {"uri": _path_to_uri(target)},
                "position": {"line": line, "character": character},
                "context": {"includeDeclaration": include_declaration},
            },
        )
    finally:
        client.close()

    refs = []
    if isinstance(result, list):
        for item in result:
            if not isinstance(item, dict):
                continue
            uri = str(item.get("uri", ""))
            rng = item.get("range", {})
            start = rng.get("start", {})
            refs.append(
                {
                    "path": _uri_to_path(uri),
                    "line": int(start.get("line", 0)),
                    "character": int(start.get("character", 0)),
                }
            )
            if len(refs) >= max_results:
                break

    _lsp_log(
        "references_success",
        thread_id,
        path=rel_path,
        language=resolved_language,
        server=command[0],
        count=len(refs),
    )
    state["lsp_status"] = "available"
    _bump_metric(state, "lsp_references_success")
    set_thread_state(thread_id, state)

    return {
        "ok": True,
        "language": resolved_language,
        "server_command": command,
        "references": refs,
        "count": len(refs),
        "truncated": isinstance(result, list) and len(result) > max_results,
    }


def handle_git_status(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    del args, config
    state = _repo_state_or_error(thread_id)
    repo_root = Path(state["repo_root"])

    status = _run_git_checked(repo_root, ["status", "--short", "--branch"])
    branch = _current_branch(repo_root)

    state["current_branch"] = branch
    set_thread_state(thread_id, state)

    return {"ok": True, "branch": branch, "status": _clip(status)}


def handle_git_diff(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    del config
    state = _repo_state_or_error(thread_id)
    repo_root = Path(state["repo_root"])

    staged = bool(args.get("staged", False))
    rel_path = str(args.get("path", "")).strip()

    git_args = ["diff"]
    if staged:
        git_args.append("--staged")
    if rel_path:
        target = _safe_repo_path(repo_root, rel_path)
        git_args.extend(["--", str(target.relative_to(repo_root))])

    diff = _run_git_checked(repo_root, git_args)
    return {
        "ok": True,
        "staged": staged,
        "diff": _clip(diff),
    }


def handle_commit_changes(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    state = _repo_state_or_error(thread_id)
    repo_root = Path(state["repo_root"])

    message = str(args.get("message", "")).strip()
    if not message:
        raise ToolError("commit_changes requires 'message'.")

    add_all = bool(args.get("add_all", True))
    if add_all:
        _run_git_checked(repo_root, ["add", "-A"])

    diff_check = _run_git(repo_root, ["diff", "--cached", "--quiet"])
    if diff_check.returncode == 0:
        return {"ok": True, "committed": False, "message": "No staged changes to commit."}
    if diff_check.returncode not in (0, 1):
        raise ToolError("Could not evaluate staged changes.")

    identity_env = _git_identity_from_config(config)
    git_user_name = identity_env.get("GIT_COMMITTER_NAME") or identity_env.get("GIT_AUTHOR_NAME")
    git_user_email = identity_env.get("GIT_COMMITTER_EMAIL") or identity_env.get("GIT_AUTHOR_EMAIL")
    commit_args = []
    if git_user_name:
        commit_args.extend(["-c", f"user.name={git_user_name}"])
    if git_user_email:
        commit_args.extend(["-c", f"user.email={git_user_email}"])
    commit_args.extend(["commit", "-m", message])

    _run_git_checked(repo_root, commit_args, extra_env=identity_env)
    commit_sha = _run_git_checked(repo_root, ["rev-parse", "HEAD"])
    branch = _current_branch(repo_root)

    state["last_commit"] = commit_sha
    state["current_branch"] = branch
    set_thread_state(thread_id, state)

    return {
        "ok": True,
        "committed": True,
        "commit": commit_sha,
        "branch": branch,
    }


def handle_push_branch(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    state = _repo_state_or_error(thread_id)
    owner, repo = _owner_repo_from_state(state)
    token = _installation_access_token(config, owner, repo)
    repo_root = Path(state["repo_root"])

    remote = str(args.get("remote", "origin")).strip() or "origin"
    branch = str(args.get("branch", "")).strip() or _current_branch(repo_root)

    _run_git_checked(repo_root, ["push", "-u", remote, branch], token=token, timeout=600)

    state["current_branch"] = branch
    set_thread_state(thread_id, state)

    return {
        "ok": True,
        "remote": remote,
        "branch": branch,
    }


def handle_create_pull_request(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    state = _repo_state_or_error(thread_id)
    owner, repo = _owner_repo_from_state(state)
    token = _installation_access_token(config, owner, repo)

    title = str(args.get("title", "")).strip()
    if not title:
        raise ToolError("create_pull_request requires 'title'.")

    checks = state.get("last_checks")
    allow_failed_checks = bool(args.get("allow_failed_checks", False))
    if (
        isinstance(checks, dict)
        and checks.get("all_passed") is False
        and not allow_failed_checks
    ):
        raise ToolError(
            "Checks failed in the last run_checks call. Ask the user for confirmation before overriding with allow_failed_checks=true."
        )

    repo_root = Path(state["repo_root"])
    head = str(args.get("head", "")).strip() or _current_branch(repo_root)
    base = str(args.get("base", "")).strip() or str(state.get("base_branch", "main"))
    body = str(args.get("body", "")).strip()
    draft = bool(args.get("draft", False))

    ahead_count = _commit_count_between(repo_root, base, head)
    if ahead_count <= 0:
        raise ToolError(
            f"No commits between {base} and {head}. Ensure changes are committed on {head} and pushed before creating a PR."
        )

    payload = {
        "title": title,
        "body": body,
        "head": head,
        "base": base,
        "draft": draft,
    }

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    data = _github_api_json(url=url, method="POST", bearer_token=token, payload=payload, timeout=30)

    state["last_pr"] = {
        "number": data.get("number"),
        "url": data.get("html_url"),
        "base": base,
        "head": head,
    }
    set_thread_state(thread_id, state)

    return {
        "ok": True,
        "number": data.get("number"),
        "url": data.get("html_url"),
        "state": data.get("state"),
        "draft": data.get("draft"),
        "base": base,
        "head": head,
    }


TOOL_HANDLERS = {
    "open_repository": handle_open_repository,
    "create_feature_branch": handle_create_feature_branch,
    "list_files": handle_list_files,
    "read_file": handle_read_file,
    "search_text": handle_search_text,
    "write_file": handle_write_file,
    "replace_in_file": handle_replace_in_file,
    "write_files": handle_write_files,
    "apply_patch": handle_apply_patch,
    "run_checks": handle_run_checks,
    "list_checks": handle_list_checks,
    "install_toolchain": handle_install_toolchain,
    "toolchain_probe": handle_toolchain_probe,
    "metrics_report": handle_metrics_report,
    "lsp_probe": handle_lsp_probe,
    "lsp_definition": handle_lsp_definition,
    "lsp_references": handle_lsp_references,
    "git_status": handle_git_status,
    "git_diff": handle_git_diff,
    "commit_changes": handle_commit_changes,
    "push_branch": handle_push_branch,
    "create_pull_request": handle_create_pull_request,
}


class CapabilityServicer(capability_pb2_grpc.CapabilityServicer):
    def Healthcheck(self, request, context):
        del request, context
        has_git = _run_command(["git", "--version"]).returncode == 0
        has_rg = _run_command(["rg", "--version"]).returncode == 0
        if not has_git or not has_rg:
            missing = []
            if not has_git:
                missing.append("git")
            if not has_rg:
                missing.append("ripgrep")
            return capability_pb2.HealthResponse(
                ready=False,
                message=f"Missing dependencies: {', '.join(missing)}",
            )
        return capability_pb2.HealthResponse(ready=True, message="ok")

    def Invoke(self, request, context):
        del context
        tool = request.tool_name
        thread_id = _normalize_thread_id(request.thread_id)
        log.info("Invoke tool=%s thread_id=%s", tool, thread_id)

        handler = TOOL_HANDLERS.get(tool)
        if handler is None:
            return capability_pb2.InvokeResponse(
                error=f"Unknown tool: {tool}. Available: {', '.join(sorted(TOOL_HANDLERS.keys()))}"
            )

        config: Dict[str, Any] = {}
        if request.config_json:
            try:
                decoded = json.loads(request.config_json)
                if isinstance(decoded, dict):
                    config = decoded
            except Exception:
                config = {}

        try:
            args = json.loads(request.args_json) if request.args_json else {}
        except Exception:
            primary = TOOL_PRIMARY_PARAM.get(tool)
            if primary:
                raw = (
                    request.args_json.decode("utf-8", errors="replace")
                    if isinstance(request.args_json, bytes)
                    else str(request.args_json)
                )
                args = {primary: raw}
            else:
                return capability_pb2.InvokeResponse(error="Invalid JSON arguments")

        if not isinstance(args, dict):
            primary = TOOL_PRIMARY_PARAM.get(tool)
            if primary:
                args = {primary: args}
            else:
                return capability_pb2.InvokeResponse(error="Arguments must be a JSON object")
        log.info("Invoke args tool=%s thread_id=%s %s", tool, thread_id, _args_shape(args))

        try:
            result = handler(args, config, thread_id)
            result_json = json.dumps(result, ensure_ascii=False)
            log.info("Tool success tool=%s thread_id=%s", tool, thread_id)
            return capability_pb2.InvokeResponse(result_json=result_json.encode("utf-8"))
        except ToolError as exc:
            if tool.startswith("lsp_"):
                _lsp_log("failure", thread_id, tool=tool, error=str(exc))
                state = get_thread_state(thread_id)
                state["lsp_status"] = "failure"
                _bump_metric(state, "lsp_failures")
                set_thread_state(thread_id, state)
            log.warning("Tool error tool=%s thread_id=%s error=%s", tool, thread_id, exc)
            return capability_pb2.InvokeResponse(error=str(exc))
        except subprocess.TimeoutExpired as exc:
            log.warning("Tool timeout tool=%s thread_id=%s error=%s", tool, thread_id, exc)
            return capability_pb2.InvokeResponse(error=f"Command timed out: {exc}")
        except Exception as exc:
            log.exception("Unhandled error in tool %s", tool)
            return capability_pb2.InvokeResponse(error=f"Tool failed: {exc}")

    def StreamInvoke(self, request, context):
        resp = self.Invoke(request, context)
        yield capability_pb2.InvokeChunk(data=resp.result_json, done=True, error=resp.error)


def serve() -> None:
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    capability_pb2_grpc.add_CapabilityServicer_to_server(CapabilityServicer(), server)
    server.add_insecure_port("0.0.0.0:50051")
    server.start()
    log.info("coding-github capability listening on :50051")
    server.wait_for_termination()


if __name__ == "__main__":
    serve()

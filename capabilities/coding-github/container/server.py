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
from pathlib import Path
from typing import Any, Dict, List
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import grpc

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

REPO_SHORTHAND_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REPO_URL_RE = re.compile(r"^https://github\\.com/([^/]+)/([^/]+?)(?:\\.git)?/?$")

TOOL_PRIMARY_PARAM = {
    "open_repository": "repository",
    "create_feature_branch": "feature",
    "read_file": "path",
    "search_text": "query",
    "lsp_definition": "path",
    "lsp_references": "path",
    "commit_changes": "message",
    "create_pull_request": "title",
}

CHECK_COMMAND_ALLOWLIST = [
    "cargo test",
    "cargo check",
    "cargo fmt --check",
    "cargo clippy",
    "npm test",
    "npm run test",
    "npm run lint",
    "npm run build",
    "pnpm test",
    "pnpm lint",
    "pnpm build",
    "yarn test",
    "yarn lint",
    "yarn build",
    "pytest",
    "ruff check",
    "mypy",
    "go test",
    "make test",
    "make lint",
    "make build",
]

LSP_SERVER_BY_LANGUAGE = {
    "rust": ["rust-analyzer"],
    "python": ["pylsp"],
    "go": ["gopls"],
    "typescript": ["typescript-language-server", "--stdio"],
    "javascript": ["typescript-language-server", "--stdio"],
    "java": ["jdtls"],
    "kotlin": ["kotlin-language-server"],
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
}

PROJECT_MARKERS = {
    "rust": ["Cargo.toml"],
    "python": ["pyproject.toml", "setup.py", "requirements.txt"],
    "go": ["go.mod"],
    "typescript": ["tsconfig.json", "package.json"],
    "javascript": ["package.json"],
    "java": ["pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"],
    "kotlin": ["build.gradle.kts", "settings.gradle.kts", "pom.xml"],
}

state_lock = threading.Lock()


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


def _require_token(config: Dict[str, Any]) -> str:
    token = str(config.get("GITHUB_TOKEN", "")).strip()
    if not token:
        raise ToolError("Missing required credential: GITHUB_TOKEN")
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
        # GitHub git-over-https expects Basic auth; PAT in Bearer header may be ignored.
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

    stripped = cmd.strip()
    return any(
        stripped == allowed or stripped.startswith(f"{allowed} ")
        for allowed in CHECK_COMMAND_ALLOWLIST
    )


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
            f"No supported LSP server found for language '{detected}'. Install one of: rust-analyzer, pylsp, gopls, typescript-language-server, jdtls, kotlin-language-server."
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

    token = _require_token(config)
    owner, repo = _parse_repository(repository)

    directory = str(args.get("directory", "")).strip()
    if directory:
        repo_root = _safe_workspace_path(directory)
    else:
        repo_root = _safe_workspace_path(f"repos/{owner}__{repo}")

    if repo_root.exists():
        if not (repo_root / ".git").exists():
            raise ToolError("Target directory exists but is not a git repository.")
        _run_git_checked(repo_root, ["fetch", "--all", "--prune"], token=token)
    else:
        repo_root.parent.mkdir(parents=True, exist_ok=True)
        clone_url = f"https://github.com/{owner}/{repo}.git"
        _run_git_checked(WORKSPACE_ROOT, ["clone", clone_url, str(repo_root)], token=token, timeout=600)

    base_branch = _detect_default_branch(repo_root, token)
    _ensure_local_branch(repo_root, base_branch)
    _run_git(repo_root, ["pull", "--ff-only", "origin", base_branch], token=token, timeout=240)

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
    token = _require_token(config)

    branch_name = _slugify_feature(feature)
    from_base = bool(args.get("from_base", True))

    if from_base:
        base = str(state.get("base_branch") or _detect_default_branch(repo_root, token))
        _ensure_local_branch(repo_root, base)
        _run_git(repo_root, ["pull", "--ff-only", "origin", base], token=token, timeout=240)
        _run_git_checked(repo_root, ["checkout", "-B", branch_name, base])
    else:
        _run_git_checked(repo_root, ["checkout", "-B", branch_name])

    state["current_branch"] = branch_name
    set_thread_state(thread_id, state)

    return {
        "ok": True,
        "branch": branch_name,
        "base_branch": state.get("base_branch"),
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


def handle_run_checks(args: Dict[str, Any], config: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    del config
    state = _repo_state_or_error(thread_id)
    repo_root = Path(state["repo_root"])

    commands = args.get("commands")
    if not isinstance(commands, list) or not commands:
        raise ToolError("run_checks requires a non-empty commands array.")

    stop_on_failure = bool(args.get("stop_on_failure", True))

    results = []
    for raw in commands:
        if not isinstance(raw, str):
            raise ToolError("Each command must be a string.")
        cmd = raw.strip()
        if not _is_allowed_check_command(cmd):
            raise ToolError(f"Command is not in the allowlist: {cmd}")

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
    token = _require_token(config)
    state = _repo_state_or_error(thread_id)
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
    token = _require_token(config)
    state = _repo_state_or_error(thread_id)

    owner = str(state.get("owner", "")).strip()
    repo = str(state.get("repo", "")).strip()
    if not owner or not repo:
        raise ToolError("Repository owner/repo is missing. Run open_repository again.")

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

    payload = {
        "title": title,
        "body": body,
        "head": head,
        "base": base,
        "draft": draft,
    }

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    req = Request(url=url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
    except HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")[:2000]
        raise ToolError(f"GitHub API error {exc.code}: {_clip(body_text, 1200)}")
    except URLError as exc:
        raise ToolError(f"Network error creating PR: {exc}")

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
    "apply_patch": handle_apply_patch,
    "run_checks": handle_run_checks,
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

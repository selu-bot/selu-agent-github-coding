# coding-github agent

Coding agent package for Selu that works with GitHub repositories.

## Highlights

- Opens or refreshes a repository from GitHub
- Creates `feature-...` branches
- Uses LSP navigation (`probe`, `definition`, `references`) for more precise edits
- Reads/searches/edits files in a workspace
- Deterministic edit tools:
  - `write_file` (`path`, `content`)
  - `replace_in_file` (`path`, `find`, `replace`, optional `replace_all`)
  - `write_files` (`files[]` batch full writes)
  - Guardrail: edit calls must include all required arguments; retries should resend complete JSON, never `{}` or partial args
- Runs allowlisted validation checks
- Commits, pushes, and creates pull requests
- Blocks PR creation by default when checks failed

## Workflow contract

The coding flow is fixed:

1. Checkout/open repository
2. Create `feature-<slug>` branch
3. Plan + clarifying questions
4. Implement changes
5. Commit changes
6. Push (approval required)
7. Create ready PR (approval required)

## Credentials

- `GITHUB_APP_ID` (user-scoped, required)
- `GITHUB_APP_PRIVATE_KEY` or `GITHUB_APP_PRIVATE_KEY_BASE64` (one required)
- `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` (optional, for commit identity)
- `GIT_COMMITTER_NAME` / `GIT_COMMITTER_EMAIL` (optional, for commit identity)
  - Backward-compatible aliases are accepted in runtime config:
    - `GITAUTHORNAME`, `GITAUTHOREMAIL`, `GITCOMMITTERNAME`, `GITCOMMITTER_EMAIL`, `GITCOMMITTEREMAIL`

## Approval defaults

- `push_branch`: ask
- `create_pull_request`: ask

## Capability profile

- class: `environment`
- filesystem: `workspace`
- network: GitHub allowlist only

## LSP support

- Included by default: Python (`pylsp`)
- Included in the container image: Rust (`rust-analyzer`), Go (`gopls`), TypeScript/JavaScript (`typescript-language-server`), Java (`jdtls`), Kotlin (`kotlin-language-server`)
- Also included for scripting/config repos: Bash (`bash-language-server`), YAML (`yaml-language-server`)

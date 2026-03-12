# coding-github agent

Coding agent package for Selu that works with GitHub repositories.

## Highlights

- Opens or refreshes a repository from GitHub
- Creates `feature-...` branches
- Uses LSP navigation (`probe`, `definition`, `references`) for more precise edits
- Reads/searches/edits files in a workspace
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

- `GITHUB_TOKEN` (user-scoped, required)

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

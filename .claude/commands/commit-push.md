Stage the user's pending work, create one good commit, and push it to the remote tracking branch.

Steps:

1. Run these in parallel and inspect the output:
   - `git status` (no `-uall`)
   - `git diff` (unstaged) and `git diff --staged` (already-staged)
   - `git log --oneline -10` to learn the repo's commit-message style
   - `git rev-parse --abbrev-ref HEAD` to learn the current branch
   - `git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null` to learn the upstream (may be empty)

2. **Safety checks** — refuse or pause and ask the user if any apply:
   - Current branch is `main` or `master`. Confirm explicitly before pushing.
   - Working tree contains files that look like secrets (`.env`, `*credentials*`, `*.pem`, `*.key`, files matching `*secret*`). Skip them by default and tell the user.
   - Untracked binaries > 5 MB. Skip and mention.
   - Nothing to commit AND nothing unpushed. Tell the user and stop.

3. **Draft the commit message**:
   - Match the style you see in recent `git log` (subject length, capitalization, tense).
   - 1–2 sentences focused on *why*, not *what*. The diff already shows the what.
   - Include the Co-Authored-By trailer below.

4. **Stage specific files** (never `git add -A` / `git add .`):
   - List the files you intend to stage and skip. Stage them by name.

5. **Commit** using a HEREDOC so multi-line messages format correctly:
   ```
   git commit -m "$(cat <<'EOF'
   <subject line>

   <optional body — 1–2 sentences explaining why>

   Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
   EOF
   )"
   ```
   - Never use `--no-verify`, `--amend`, `--no-gpg-sign`, or `-c commit.gpgsign=false`.
   - If a pre-commit hook fails: fix the issue, re-stage, and create a **new** commit (do not amend).

6. **Push**:
   - If upstream is set: `git push`.
   - If no upstream: `git push -u origin <current-branch>`.
   - **Never** `--force` or `--force-with-lease` unless the user explicitly asked. If on `main`/`master`, do not force-push at all.

7. **Report back**:
   - The commit SHA + subject line that landed.
   - The branch + remote it pushed to.
   - If a PR could be opened (no PR exists for this branch on GitHub), mention the URL `gh pr create` would target — but do NOT open the PR; that's a separate action.
   - If the push was skipped (e.g., main branch confirmation pending), say so.

Notes:
- Run `git` commands directly — never prepend `cd <current-directory>` (that triggers permission prompts).
- Quote paths with spaces.
- If the diff is very large, summarize it in one sentence rather than dumping it into the commit body.

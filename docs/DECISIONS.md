# Decisions

Project decisions, newest last. Each entry: date, the decision, why, and what was rejected.
Cross-project standards live in https://github.com/ki4hrg/standards and are not repeated here;
the entries below are this repo's exceptions to them.

## 2026-11-14 — `monitor.py` stays a single file at the repo root

The Python service profile asks for `src/<package>/` with a console-script entry point. This
app is one module with one dependency, and the move is Phase 6 (stack) work, which is out of
scope for the standards sweep. `pyproject.toml` declares no build system and pytest imports
`monitor` from the root. Rejected: the `src/` layout now, as churn without a gain.

## 2026-11-14 — Unversioned, though the repo is public

Merging to `main` is the release; there are no `v*` tags or semver image tags (the old
workflow's tag trigger was dropped). Going public didn't make it a released tool: nobody else
installs it or pulls the image into another stack. Rejected: versioning it like a distributed
artifact. Revisit if the image is published for other people.

## 2026-11-14 — Git history kept after the hostname removal

The repo went public with the deploy host's LAN machine name in a workflow comment and one
commit message. Phase 1 removed the comment; the commit message stays. It's a name, not a
credential, the repo was already public and cloneable, and a rewrite changes every later SHA.
gitleaks over the full history, commit messages and PR text found no secrets. Rejected:
rewriting history.

## 2026-11-14 — No `/api/health`: the revision is checked by hand

The app serves no HTTP, so there's no `/api/health` and no `APP_HEALTH_URL`, and the deploy job
can't check the running revision. The Dockhand stack has Force redeploy and Re-pull images on,
so `success: true` means a real redeploy, and a skipped redeploy still fails the job. After a
deploy, the revision is checked by hand from the container's
`org.opencontainers.image.revision` label (and, from Phase 2, the startup log line). Liveness is
the compose healthcheck on a heartbeat file that only the scheduling loop touches. Rejected: an
HTTP server only for the check.

## 2026-11-14 — `docker-compose.yml` keeps its name

The standard name is `compose.yaml`, but the Dockhand stack tracks this path; renaming it means
repointing the stack at the same time. Rejected: renaming in the sweep.

## 2026-11-14 — Formatting commit

The `ruff format` commit is listed in `.git-blame-ignore-revs`. The Phase 1 PR that holds it is
merged with a merge commit, not squashed, so that commit's SHA reaches `main` unchanged.

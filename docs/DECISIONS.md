# Decisions

Project decisions, newest last. Each entry: date, the decision, why, and what was rejected.
Cross-project standards live in https://github.com/ki4hrg/standards and are not repeated here;
the entries below are this repo's exceptions to them.

## 2026-11-14 — `monitor.py` stays a single file at the repo root

The Python service profile asks for `src/<package>/` with a console-script entry point. This
app is one module with two dependencies (`apprise` and `pydantic-settings`), and the move is
Phase 6 (stack) work, which is out of scope for the standards sweep. `pyproject.toml` declares no build system and pytest imports
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
the compose healthcheck on a heartbeat file that only the scheduling loop touches (and, from
Phase 3, its retry waits: a planned pause, not a hang). Rejected: an HTTP server only for the
check.

## 2026-11-14 — `docker-compose.yml` keeps its name

The standard name is `compose.yaml`, but the Dockhand stack tracks this path; renaming it means
repointing the stack at the same time. Rejected: renaming in the sweep.

## 2026-11-14 — Formatting commit

The `ruff format` commit is listed in `.git-blame-ignore-revs`. The Phase 1 PR that holds it is
merged with a merge commit, not squashed, so that commit's SHA reaches `main` unchanged.

## 2026-11-15 — A failed check is retried, notified once per kind, and recovered from

John's answers on the Phase 3 PR (#14), after the audit's Q10. The exact behaviour is in
`docs/DEPLOYMENT.md` (§ When a check fails, § Stopping); this entry records the choices.

- **Retries within the check.** A network error, HTTP 5xx or 429 is retried after 60 s, then
  300 s, from one 6-minute budget per check; any other 4xx fails at once. Before, any YNAB
  error exited the container, and with a daily schedule that day's check was lost. A retry
  can't fix a revoked token, so that fails at once.
- **`Retry-After` on a 429** sets the wait, capped at 300 s and within the budget; a longer ask
  fails the check. YNAB allows 200 requests an hour, so a longer wait belongs to the next run.
  Only a 429 reads the header; a 503 gets the fixed waits.
- **Notices.** The first failure is notified, then again when the kind changes (so a revoked
  token mid-outage isn't muted) or when the last notice couldn't be sent (so no failure goes
  unheard); the first check that finishes after a notified failure sends one recovery notice.
  The state is in memory, so a restart resets it.
- **Apprise URLs are checked at boot**: one that doesn't load fails the boot, named by variable
  and entry number, instead of showing up only when a notice is due.
- **`stop_grace_period: 45s`**: a stop lets the one YNAB request in flight finish, and it can
  take up to 30 s; Docker's default is 10 s.
- **The heartbeat is written during retry waits** (see the liveness entry above): a wait is a
  planned pause of up to 6 minutes, while a hung request must still go stale.

Rejected: exiting on a YNAB error (the old behaviour); keeping the scheduling loop as the only
heartbeat writer and widening the healthcheck window to the worst-case check.

## 2026-11-15 — The standards sweep is closed

Phases 0–4 are done and deployed (#10, #13, #14, #15); Phase 5 doesn't apply (no UI) and
Phase 6 is out of scope. What landed, how each deploy was checked, the gaps left open and
John's open items are in `docs/plans/standards-conformance.md` § Close-out.

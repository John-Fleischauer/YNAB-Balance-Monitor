# Standards conformance: YNAB-Balance-Monitor

Audited 2026-11-14 against the standards at `v1` (`aca66f4`); the local standards clone is at
`d71b550` (docs past `v1`). Profile: **Python service** (`docs/profiles/python-service.md`),
template `templates/python-service/` (only `.github/workflows/ci.yml`). This is the Phase 0
report, committed with the Phase 1 PR.

Repo `John-Fleischauer/YNAB-Balance-Monitor` is **public** and owned by John's organization (it
moved there from his personal account just before this audit). `main` = `c28d749` (#9). No
production reads: nothing was run against YNAB, the notification services, the deploy host or
Dockhand.

## Status

| Phase | Status | PR | Merge |
|---|---|---|---|
| 0 Audit | done (this file) | | |
| 1 Pipeline (+ uv, ruff, first tests) | done | #10 | by hand |
| 2 Runtime | done | #13 | by hand, John watches |
| 3 Boundaries (narrow) | in review | `fix/boundaries` | `automerge` |
| 4 Agent docs | proposed | | `automerge` |
| 5 Design/copy | **n/a**: no UI (notifications only) | | |
| 6 Stack | out unless John asks (Q7) | | |

## The audit

- **What it is:** one Python file (`monitor.py`, 662 lines) that reads a YNAB budget, projects
  the minimum balance of one or more accounts to the end of the month (or `MONITOR_DAYS`), and
  sends Apprise notifications: an alert below a threshold, and routine updates. It runs as a
  long-lived container with its own scheduler (`SCHEDULE`, `UPDATE_SCHEDULE`), or once and exits.
  No HTTP server, no database, no volumes. Inputs: environment variables and YNAB API responses.
  Outputs: outbound HTTPS to the YNAB API and to notification services.
- **Activity:** 28 commits (2026-02 to 2026-10). 15 are authored "Claude", the rest are John's
  merges and edits. In the last 90 days: 2 non-merge commits (`00745d1`, `4217f91`), both with a
  Claude co-author trailer.
- **Deploy today:** `.github/workflows/build.yml` builds `linux/amd64` and pushes
  `ghcr.io/ki4hrg/ynab-balance-monitor` (`:main`, `:sha-…`, semver on `v*` tags), then calls the
  Dockhand git-stack webhook with the `success: true` check. Compose pulls with
  `pull_policy: always`, no `build:` ✓.
- **Repo settings (read with `gh`):** secrets `DOCKHAND_WEBHOOK_TOKEN`, `DOCKHAND_WEBHOOK_URL`; no
  variables (`APP_HEALTH_URL` unset, correct: there is no HTTP endpoint); Actions enabled; last
  runs (2026-10-17, before the move) started and passed.
- **Checks run here** (throwaway venv inside the repo, removed afterwards):
  - `pip-audit` on the resolved dependencies of `requirements.txt` (apprise 2.2.0, requests
    2.34.2, urllib3 2.8.0, certifi, idna, pyyaml, markdown, click, oauthlib,
    requests-oauthlib, charset-normalizer): **no known vulnerabilities**. Phase 1's audit gate
    should go green.
  - `ruff check` (defaults): 5 findings (an unused import, an unused variable, import order, a
    collapsible `if`, a shebang on a non-executable file). `ruff format --check`: 1 file would be
    reformatted. Both become CI gates in Phase 1 (`python-check.yml`).
  - `python -m py_compile monitor.py`: compiles on 3.13.
  - No tests exist, so none ran.
  - **History scan:** gitleaks was not available (installing it was refused in this session).
    Substitute: the full patch history (`git log -p --all`) and all PR bodies and comments were
    searched for IP addresses, hostnames, LAN domains, emails, token prefixes (`ghp_`,
    `github_pat_`, `xox`, `AKIA`, private keys), UUIDs (YNAB account and budget IDs) and long
    high-entropy strings. Findings are in the next section. Running gitleaks is still owed
    (`decisions.md`, "Before a private repo goes public"; Q2).
  - **gitleaks (Phase 1 session, 2026-11-14, official image):** full history (every non-merge
    commit), commit messages, PR bodies, comments and reviews, issues, and the working tree: no
    leaks found. gitleaks looks for credentials, not hostnames; the hostname in `00745d1`'s
    message stays, per Q2.

## Critical and security flags (read these first)

1. **A private hostname is published.** The repo went public with the deploy host's machine
   name in two places:
   - tracked file `.github/workflows/build.yml:37` (a comment naming the host as x86_64);
   - the commit message of `00745d1` ("build/push the image to GHCR (linux/amd64 for …)").

   It's a LAN name, not a credential, and it's in no PR body or comment. Phase 1 removes the
   file. Rewriting public history to remove the commit message is John's call (Q2). No other
   hostname, IP, account ID, notification topic or token was found in files, history or PRs;
   `.env.example` and the README hold placeholders only. Commit messages carry Claude session
   links, which are private to John's account (not a leak, noted for completeness).
2. **The next push to `main` won't deploy correctly.** `build.yml:29` pushes to
   `ghcr.io/ki4hrg/ynab-balance-monitor` and `docker-compose.yml:5` pulls it, but the repo is
   now in the organization. Its `GITHUB_TOKEN` probably can't push to the old package, so the
   run goes red, or it pushes to the old path and the deploy keeps running the old image while
   reporting success. **Phase 1** repoints both to `ghcr.io/john-fleischauer/ynab-balance-monitor`.
   The new package starts private (Q3), and the deploy host needs to be able to pull it.
3. **The container runs as root** (`Dockerfile` has no `USER`). **Phase 2.**
4. **Nothing gates the image or the merge.** `build.yml` runs on `pull_request` ✓, but it only
   builds the image: no lint, no tests, no dependency audit. PRs were merged by hand. **Phase 1.**
5. **Security settings are off on a public repo:** Dependabot alerts (404), Dependabot security
   updates, GitHub secret scanning and push protection are all disabled; there's no
   `dependabot.yml`. GitGuardian checked `4217f91` (#9) under the old owner; whether the
   organization's installation covers the repo now is unchecked (Phase 1 checklist item).
6. **An Apprise URL prefix is logged.** `monitor.py:387` prints the first 40 characters of a URL
   that fails to load. Apprise URLs carry their credentials in the URL (`tgram://bot-token/…`,
   `discord://webhook-id/webhook-token`), so a typo prints part of a secret to the container
   log. `security.md`: secrets never appear in logs. **Phase 3.**
7. **No secret guards.** Compose loads `env_file: .env`, so nothing fails the deploy when
   `YNAB_API_TOKEN` or `APPRISE_URLS` is missing (the app exits at boot instead, and
   `restart: unless-stopped` restarts it in a loop). **Phase 2** (Q4).

## Summary

| Area | Status | Top gaps |
|---|---|---|
| Workflow | partial | CI on `pull_request` ✓ but image-only; hand-rolled jobs on `ubuntu-latest`; `packages: write` at workflow level, PRs included; no merge-on-green, close-issues, `automerge`/`agent-found` labels; merge, rebase and squash all allowed, branches not deleted on merge; no PR template; PR titles not Conventional Commits (#1–#9); `v*` tag trigger with semver tags on an unversioned app |
| Quality | missing | No `pyproject.toml`, no ruff, no tests, no lint or test in CI |
| Security | partial | Audit clean today but not run in CI; no Dependabot; alerts, security updates, secret scanning and push protection off; root container; no compose guards; Apprise URL prefix logged. Meets: env read at boot with a `validate_config` check (hand-rolled, not `pydantic-settings`); `.env` gitignored; `.env.example` has placeholders only |
| Deploy | partial | Image built in CI ✓, no `build:` ✓, `pull_policy: always` ✓, `:main` + `:sha-` tags and OCI revision label (metadata-action) ✓, webhook GET with `?secret=` and `success: true` check ✓, heartbeat healthcheck ✓. Gaps: old image path after the move; no `GIT_REVISION` in the image or the log; not the reusable `dockhand-deploy.yml`; compose file is `docker-compose.yml` (stack tracks that path, keep); no `docs/DEPLOYMENT.md`. `/api/health`, Postgres, `PGPASSWORD`, migrations: **n/a** |
| Stack | differs | `requirements.txt` + pip (profile: uv with `uv.lock`); `python:3.13-alpine` (profile: `-slim`, uv copied in); single file at the root (profile: `src/<package>/` + console script); hand-rolled config (profile: `pydantic-settings`); apprise upper-bounded `<3` ✓; Python 3.13 matches the profile's stated version |
| Design | **n/a** | No UI. Notification text is out of scope for `design.md` |
| Agent docs | partial | CLAUDE.md 28 lines ✓, but **states the `stack.env`/Portainer setup that John replaced** (`fd41425`); README describes the old ntfy notifier and one account; `.env.example` and README omit `YNAB_ACCOUNT_ID_CC`/`_NO_CC`; no `docs/`, `.claude/settings.json`, `claude-candidates.md`; no plans referenced outside the repo ✓ |

## Details

### Workflow

- `build.yml` triggers on `push` to `main`, `v*` tags and `pull_request` ✓ (`build.yml:3–7`). No
  `concurrency`, no `workflow_dispatch`, runners `ubuntu-latest` (standard: `ubuntu-24.04`).
- `permissions: packages: write` is workflow-wide (`build.yml:9–11`), so PR runs get it too.
- The deploy job is hand-rolled but already does what the standard asks: plain GET, `?secret=`,
  fails on non-2xx and unless `.success == true` (`build.yml:46–104`). It also accepts
  `WEBHOOK_URL`/`WEBHOOK_TOKEN` and variables as fallbacks, and silently skips when no URL is set.
  `dockhand-deploy.yml` replaces it, taking the two secrets by name (they exist with the standard
  names ✓).
- Merge settings: merge commit, rebase and squash all allowed; `delete_branch_on_merge` false;
  only the default labels. `scripts/repo-settings.sh` fixes these.
- **Public repo:** PR jobs run on GitHub's runners and minutes are free. The template's `runs-on`
  expression keeps a public repo off the self-hosted runner by itself; **`PR_RUNNER`,
  `MAIN_RUNNER` and `IMAGE_RUNNER` must stay unset** (`decisions.md`: the runner never serves a
  public repo).

### Quality

- No test runner, no linter, no formatter, no type checking. `python-check.yml@v1` runs
  `uv sync --locked`, `ruff check`, `ruff format --check` and `pytest` unconditionally, with no
  input to leave any out. So Phase 1 can't adopt it without a `pyproject.toml`, `uv.lock`, a
  clean ruff run and at least one test (standards gap 1). The profile asks for "at least one test
  of its pure logic" anyway.
- Good first test targets, all pure: `_expand_occurrences` (12 frequencies plus `never`),
  `_add_months` (month-end clamping), `project_minimum_balance` (CC payments already covered by
  a scheduled transfer aren't counted twice), `_parse_account_entries`, `_parse_schedule`.
- No database, so "tests on real Postgres" is n/a.

### Security

- `pip-audit`: clean (above). The reusable `security.yml` audits from `uv.lock`, so it too needs
  Phase 1's uv move.
- Dependabot: none. Ecosystems: `uv`, `docker`, `github-actions`.
- `${VAR:?}`: none; compose uses `env_file: .env` (`docker-compose.yml:7`). Q4.
- Non-root: no (`Dockerfile`). The app writes only `/tmp/heartbeat`, which any user can, and
  there are no volumes, so no host `chown` is needed.
- Env validated at boot: partly. `validate_config` (`monitor.py:451`) checks the token, accounts
  and `APPRISE_URLS`, but `int()` on `MIN_BALANCE`, thresholds and `MONITOR_DAYS`
  (`monitor.py:84, 99, 142`) raises a traceback on a typo, and `MONITOR_DAYS` only does so when a
  cycle runs. Phase 2 replaces it with a `pydantic-settings` class.
- Secrets in logs: the Apprise URL prefix (flag 6). The YNAB token never is: it goes in the
  `Authorization` header (`monitor.py:112`), and the API's error body is printed, which doesn't
  echo it.
- Workflow permissions: see Workflow.

### Deploy

- **Mode A** (Dockhand git stack), webhook ✓. Whether the stack has Force redeploy and Re-pull
  images on is John's answer (Q1).
- **Revision:** the image carries `org.opencontainers.image.revision` (metadata-action labels) ✓,
  but there's no `GIT_REVISION` build arg or `ENV`, and the app never reports what it runs.
  There's no HTTP server, so `/api/health` and `APP_HEALTH_URL` don't apply (like a worker in
  `deploy.md`, Several images): the revision is checked by hand from the container label after
  each deploy. Phase 2 adds `GIT_REVISION` and logs it at startup, so `docker logs` shows it too.
- **Health:** the compose healthcheck reads a heartbeat file that only the scheduling loop
  touches (`monitor.py:550–559`, `docker-compose.yml:11–16`), so a hung cycle goes stale. That's
  the right shape for a service with no HTTP (standards gap 3). In run-once mode the container
  exits and the check doesn't apply. `urlopen` has no timeout (`monitor.py:114`), so a hung YNAB
  call blocks forever; Docker only marks it unhealthy (Phase 3).
- **A YNAB or network error exits the process** (`ynab_get`, `monitor.py:116–122`,
  `sys.exit(1)`), in scheduled mode too. Docker restarts it, and with a daily `SCHEDULE` the
  restarted loop waits for the next day's time, so that day's check is lost without a
  notification. Not a standards rule as such, but it's the "one place handles unexpected errors"
  part of Phase 3 for a service like this (Q10).
- `tags: ["v*"]` and `type=semver` (`build.yml:6, 32`): app repos are unversioned (Q11).
- Compose file name: `docker-compose.yml`. The standard is `compose.yaml`, but the Dockhand stack
  tracks this path; rename only together with the stack (Q15).
- `docs/DEPLOYMENT.md` missing: mode A, `linux/amd64`, no inbound exposure (no ports, no
  proxy), outbound only to YNAB and the notification services, no data to back up, revision
  checked by hand.

### Stack

Migration work, not defects: uv + `pyproject.toml` + `uv.lock` (forced into Phase 1 by
`python-check.yml`), `-slim` base with uv from its official image (Q6), `pydantic-settings`
(Phase 2), `src/ynab_balance_monitor/` with a console-script entry point (Q7). The only runtime
dependency is `apprise>=2,<3`, upper-bounded ✓.

### Agent docs

- `CLAUDE.md` 28 lines ✓. Stale: "Config: Environment variables via `stack.env` (Portainer) or
  `.env`" (line 11) and "`stack.env` in docker-compose.yml for Portainer compatibility" (line 25).
  An agent added `stack.env` in `e3a057c`; John changed it back to `.env` in `fd41425`, and the
  CLAUDE.md written later (`7f07661`, by an agent) still describes it.
- `README.md`: says it sends **ntfy** alerts (line 3) though the app uses Apprise (since
  `e2588b7`); describes one checking account; its table marks `YNAB_ACCOUNT_ID` required and
  omits `YNAB_ACCOUNT_ID_CC`/`YNAB_ACCOUNT_ID_NO_CC`; line 7 says updates go out only on the
  update schedule, but with no `UPDATE_SCHEDULE` an update goes out on every check (`monitor.py:652`,
  deliberate since #4); the example output's last line is from the ntfy version.
- `.env.example`: lacks the multi-account variables.
- Every path CLAUDE.md names exists ✓, except that `.github/workflows/build.yml` goes away in
  Phase 1.

## CLAUDE.md triage

**Provenance:** written by an agent in `7f07661` (2026-02-08, PR #1); the heartbeat and image
lines added in `00745d1` (2026-10-10, John with a Claude co-author trailer). None of it came from
an incident except the `stack.env` lines, which John's own commit `fd41425` contradicts. The
heartbeat rule appears in one other repo of John's, in different words (both from agent sessions).

### Drop (repeats or contradicts a standard, or stale)

| # | Rule | Why |
|---|---|---|
| D1 | "Config: Environment variables via `stack.env` (Portainer) or `.env`" (line 11, the `stack.env` part) | Stale: John replaced `stack.env` with `.env` (`fd41425`). Keep "environment variables via `.env`" as a fact |
| D2 | "`stack.env` in docker-compose.yml for Portainer compatibility" (line 25) | Same; Portainer isn't the deployer (Dockhand is) |
| D3 | "Image is built and pushed to GHCR by `.github/workflows/build.yml`; compose only pulls (`pull_policy: always`), no `build:` block" (line 27) | Repeats `deploy.md`; the file name changes in Phase 1 |

### Move to docs

| # | Content | Where |
|---|---|---|
| M1 | Project overview (line 5) | README already says it; CLAUDE.md keeps one line |
| M2 | Single file, only dependency `apprise`, stdlib `urllib`/`json`/`calendar` (lines 9, 23) | `docs/ARCHITECTURE.md`; one layout-map line in CLAUDE.md |
| M3 | Long-lived service with built-in scheduling; `SCHEDULE` formats `HH:MM` / `Nh` (lines 10, 26) | `docs/ARCHITECTURE.md` § Scheduling; README table already has the formats |
| M4 | YNAB API facts: milliunits, `date_next`/`date_first`, 200 requests/hour (line 15) | `docs/ARCHITECTURE.md` § YNAB API |
| M5 | Recurrence expansion for 13 frequency types (line 16) | `docs/ARCHITECTURE.md` § Projection |
| M6 | CC payment deduplication, unscheduled CC payments applied on day 1 (line 17) | `docs/ARCHITECTURE.md` § Projection |
| M7 | Multi-account config `YNAB_ACCOUNT_ID_CC` / `_NO_CC`, `id:threshold`, legacy `YNAB_ACCOUNT_ID` + `MIN_BALANCE`, budget-wide data fetched once per cycle (line 18) | `docs/ARCHITECTURE.md` § Accounts; the variables also into README and `.env.example` |
| M8 | Day-by-day walk to the minimum (line 19) | `docs/ARCHITECTURE.md` § Projection |
| M9 | `python -u` for unbuffered output, needed for container logs (line 24) | `docs/ARCHITECTURE.md` § Container, until Phase 2 replaces it with `ENV PYTHONUNBUFFERED=1`; then the fact is that env line (C2) |

### Candidates (John decides: yes keeps one line in CLAUDE.md, no leaves it in docs only)

| # | Rule | Evidence |
|---|---|---|
| C1 | **The heartbeat is touched only from the scheduling loop, never from `run_cycle`**, so a hung cycle lets it go stale; the healthcheck window is 10 minutes (line 28) | Added in `00745d1` (John, Claude trailer) with the healthcheck; not after an incident. Easy to break: the obvious "fix" for a stale heartbeat is to touch it inside the cycle, which hides exactly the hang it exists to catch. Recommend **yes** (Q8) |
| C2 | **Container output stays unbuffered** (`python -u` today, `PYTHONUNBUFFERED=1` after Phase 2) | Agent commit `a1b72d8`, no incident recorded; without it `print` output doesn't reach `docker logs` until the buffer fills. Recommend **yes**, and raise it for the profile (gap 6) (Q9) |

## Fix list (ordered by harm, rough effort)

1. **Image path after the move** (XS, Phase 1): `ghcr.io/john-fleischauer/ynab-balance-monitor`
   in the caller (default) and compose; new package visibility (Q3).
2. **Pipeline** (M, Phase 1): template `ci.yml`; `pyproject.toml` + `uv.lock` (requirements.txt
   removed, Dockerfile installs with `uv sync --locked --no-dev`); ruff fixes and format;
   first pytest tests; Dependabot; repo settings, alerts, secret scanning; hostname comment gone.
3. **Root container, secret guards, boot validation, revision** (S, Phase 2).
4. **Error handling and secret-free logs** (S, Phase 3): no Apprise URL in logs; timeouts; one
   place that logs an unexpected cycle error and keeps the scheduler running (Q10).
5. **Agent docs** (S, Phase 4): CLAUDE.md per the triage, `docs/ARCHITECTURE.md`, README and
   `.env.example` brought up to date.

## Prerequisites before Phase 1

- Q1 (Dockhand settings), Q2 (history), Q3 (package visibility), Q5 (Phase 1 scope) answered.
- GitGuardian: the organization's installation includes this repo (`checklist.md`, the move).
- No `PR_RUNNER` / `MAIN_RUNNER` / `IMAGE_RUNNER` on this repo (public).
- The deploy host can pull the new package (public package, or registry credentials in Dockhand).
- `scripts/repo-settings.sh John-Fleischauer/YNAB-Balance-Monitor` after PR 1 merges (squash only,
  `PR_TITLE`, delete branch on merge, `automerge`/`agent-found` labels, alerts, security updates,
  secret scanning and push protection, which are free on a public repo).

## Proposed scope and PR plan (to agree with John)

Proposed scope: Phases 1–4; 5 is n/a; 6 out except what Phase 1 needs (uv) and what Phase 2
rewrites anyway (Q6). Four PRs: the safety phases stay apart from the refactor and the docs.

| # | PR | Contents | Touches workflows | Merge |
|---|---|---|---|---|
| 1 | **Phase 1** `ci: standard pipeline with uv, ruff and first tests` | `ci.yml` from the template (check, security with `uv`, image, automerge, close-issues, deploy via `dockhand-deploy.yml`, secrets by name), replacing `build.yml` (removes the hostname comment, the `v*` trigger per Q11, `ubuntu-latest`); `pyproject.toml` (`requires-python` 3.13, `apprise>=2,<3`, dev group `ruff`, `pytest`) + `uv.lock` + `.python-version`; `requirements.txt` removed; Dockerfile installs with uv (base per Q6); `ruff` fixes and `ruff format` as their own commit; tests of the pure logic (above); `.github/dependabot.yml` (`uv`, `docker`, `github-actions`; monthly, grouped; ignores `ki4hrg/standards*` and Python image majors) (Q12); compose `image:` default to the new path; PR template; `docs/claude-candidates.md`; this plan | **yes** | **John by hand**, watches the first deploy (image build changed and image path moved). Then `repo-settings.sh`, check GitGuardian shows on the next PR, check the container's revision label |
| 2 | **Phase 2** `fix: run as non-root and validate config at boot` | Dockerfile `USER` (non-root), `ARG`/`ENV GIT_REVISION`, `ENV PYTHONUNBUFFERED=1`, `.dockerignore`; startup log line with the revision; `pydantic-settings` `Settings` replacing the module-level `os.environ` reads and `validate_config` (same variables and defaults, readable errors instead of tracebacks); compose guards per Q4; `docs/DEPLOYMENT.md` | no | **John by hand**, watches the deploy. Verify: `docker exec … id` not root; heartbeat healthy; a missing `YNAB_API_TOKEN` fails `compose up` with its message; the logged revision is the merged commit |
| 3 | **Phase 3** `fix: keep secrets out of logs and survive API errors` | Remove the Apprise URL from the load-failure message (say which entry number failed); `timeout=` on `urlopen`; one handler around each scheduled cycle that logs an unexpected error and keeps the loop (Q10), with `ynab_get` raising instead of `sys.exit`; tests for those refusals; `/security-review` | no | `automerge` |
| 4 | **Phase 4** `docs: agent docs and README` | CLAUDE.md per the triage; `docs/ARCHITECTURE.md`; README (Apprise, several accounts, update behaviour, example output); `.env.example` with every variable; `.claude/settings.json` (standards plugin, `autoUpdate`); `AGENTS.md` pointer; fact-check by a fresh reviewer; LICENSE per Q13 | no | `automerge` (docs-only: no rebuild or redeploy) |

**Why not fewer PRs:** PR 1 changes workflows, so it's a hand merge regardless; PR 2 changes how
the container boots, so John watches it; Phase 3 is small and could ride in PR 2 if John prefers
(Q14). Actions minutes are free for a public repo, so the split costs nothing but merges.

### Phases that don't apply

- **Phase 5 (design and copy): n/a.** No UI; the only output is notification text and logs.
- **Node 24: n/a.** No Node.
- **Postgres, `PGPASSWORD`, migrations: n/a.** No database.
- **`/api/health`, `APP_HEALTH_URL`: n/a.** No HTTP server; revision checked by hand from the
  image label and the startup log line (recorded in `docs/DEPLOYMENT.md`).
- **Self-hosted runner: n/a.** Public repo.

### Exceptions to record in `docs/DECISIONS.md` (if John agrees)

- No `/api/health`: a scheduled job with no HTTP server; heartbeat-file healthcheck and a
  hand-checked revision instead.
- `docker-compose.yml` keeps its name while the Dockhand stack tracks it (Q15).
- Single-file layout kept until Phase 6 (Q7).

## Standards gaps

1. **`python-check.yml` can't be adopted in steps.** It always runs `uv sync --locked`, both
   ruff checks and `pytest`, and `pytest` fails with no tests (exit 5). The checklist's "set the
   check's `scripts` to the ones the repo has now" has no Python equivalent, so Phase 1 for any
   Python repo also has to add uv, a ruff-clean tree and a test. Either document that in
   `checklist.md` (Other profiles) or give the workflow an input like `node-check`'s `scripts`.
2. **The Python template has only `ci.yml`.** No `dependabot.yml` (with `uv`), PR template,
   `.claude/settings.json`, `Dockerfile`/`.dockerignore`, `compose.yaml` or `pyproject.toml`
   starter, unlike the Next.js and Laravel templates.
3. **No standard for a container without HTTP.** `deploy.md` says a deploy isn't done until the
   app reports its commit, and the healthcheck is `/api/health`. A scheduled job has neither. This
   repo's heartbeat file (touched only by the scheduler) plus the image label is a reasonable
   pattern; the profile only says "`HEALTHCHECK` if the service serves HTTP".
4. **`env_file:` and `${VAR:?}` guards.** The standards don't mention `env_file`, which bypasses
   compose's guards, nor how a deploy supplies variables to a Dockhand stack. They also can't
   express "one of these three variables is required" (the account variables here); that's left
   to boot validation.
5. **Python version policy.** The profile says "current stable minus one (3.13 today)". Python
   3.15 was due in October 2026, which would make 3.14 the target; there's no rule like Node 24's
   "upgrade in every sweep" for Python.
6. **Unbuffered output.** A Python container needs `PYTHONUNBUFFERED=1` (or `-u`) for its logs to
   appear; the profile's Image section doesn't say so.
7. **`pip-audit` has no severity threshold.** `security.yml`'s uv path fails on any advisory,
   while `security.md` says high and above fails; and the per-advisory ignore the audit rule
   allows (`--ignore-vuln`) has no input.
8. **Ruff format adoption.** The Prettier rule (formatting-only PR, `.git-blame-ignore-revs`) has
   no Python counterpart, but `python-check.yml` makes `ruff format --check` a gate from Phase 1.
9. **Going public after a move.** The checklist covers moving into the organization; the
   decision to scan history before going public is in `decisions.md` only. A line in the
   checklist's move step (gitleaks over history, PR bodies and commit messages; turn on secret
   scanning; no runner variables) would have caught the hostname before publication.

## Questions for John

1. **Dockhand:** does this repo's stack have **Force redeploy** and **Re-pull images** on?
   Recommend: turn them on if not. With no `APP_HEALTH_URL`, they're what makes `success: true`
   mean a real redeploy.
2. **The published hostname:** remove the comment in Phase 1 and leave history alone, or rewrite
   history to drop it from `00745d1`'s message? Recommend leave history: it's a LAN name with no
   credential, the repo is already public and cloneable, and a rewrite changes every SHA after
   it. Also run gitleaks over the history once (it couldn't be installed here); recommend yes, in
   the Phase 1 session.
3. **New GHCR package:** make `ghcr.io/john-fleischauer/ynab-balance-monitor` public, or give
   Dockhand registry credentials? Recommend public: the source is public and the image holds no
   secrets. Delete the old `ki4hrg` package after the first deploy from the new path is verified?
   Recommend yes.
4. **Secret guards:** how does the Dockhand stack supply the variables today (a `.env` file it
   writes beside the compose file, or its own environment)? Recommend replacing `env_file: .env`
   with an `environment:` list: `${YNAB_API_TOKEN:?…}` and `${APPRISE_URLS:?…}` required,
   `${VAR:-}` for the optional ones, with the account variables checked by `pydantic-settings` at
   boot. Works if Dockhand provides them for interpolation either way.
5. **Phase 1 scope:** bring uv (`pyproject.toml`, `uv.lock`), the ruff fixes and format, and the
   first tests into the Phase 1 PR, since `python-check.yml` requires all of them? Recommend yes,
   as one hand-merged PR John watches deploy.
6. **Base image:** move from `python:3.13-alpine` to `python:3.13-slim` (the profile's), in Phase 1
   with the uv Dockerfile change? Recommend yes; stay on 3.13 until the standards settle the
   Python version (gap 5).
7. **Layout:** keep the single `monitor.py` at the root (Phase 6's `src/` package and console
   script out of scope)? Recommend yes, recorded as an exception.
8. **Candidate C1** (heartbeat only from the scheduling loop): keep as a CLAUDE.md rule?
   Recommend yes.
9. **Candidate C2** (container output unbuffered): keep? Recommend yes.
10. **Behaviour change in Phase 3:** in scheduled mode, a YNAB or network error logs and waits for
    the next run instead of exiting the container (and losing that day's check)? Optionally send a
    failure notification. Recommend yes to the first; the notification only if you want it.
11. **Versioning:** treat it as an unversioned app (drop the `v*` tag trigger and semver tags),
    not a released tool for others? Recommend unversioned, unless you mean to publish the image
    for other people now that it's public.
12. **Dependabot cadence:** monthly and grouped like private repos, or weekly (allowed for public
    repos)? Recommend monthly: each merge is a deploy.
13. **LICENSE:** the repo is public with no license (all rights reserved by default). Add one
    (MIT), or leave it? Recommend MIT if you're happy for others to reuse it; otherwise leave it
    and say so in the README.
14. **Phase 3 in its own PR or with Phase 2?** Recommend its own PR: Phase 2 is a watched boot
    change; Phase 3 can automerge.
15. **Compose file name:** keep `docker-compose.yml` (the stack tracks it)? Recommend keep;
    renaming needs the stack repointed at the same time.
16. **Scope:** Phases 1–4 in four PRs in the order above, Phase 5 n/a, Phase 6 out? Recommend yes.

## John's decisions (2026-11-14)

1. **Dockhand (Q1):** the stack has Force redeploy and Re-pull images turned on.
2. **Published hostname (Q2):** remove the comment in Phase 1 and leave history alone. Run gitleaks over the full history once, in the Phase 1 session.
3. **GHCR (Q3):** make the new package public. Delete the old ki4hrg package once the new image is serving.
4. **Secrets (Q4):** the stack supplies the variables through Dockhand's stack environment, which interpolates `${VAR}`. Compose switches from `env_file: .env` to an `environment:` list:
   - `${VAR:?…}` for required values;
   - `${VAR:-}` for optional ones;
   - the account variables are validated at boot.
5. **Errors (Q10):** in scheduled mode, a YNAB or network error is logged and the loop waits for the next run. A failure notification also goes through Apprise.
6. **Licence (Q13):** MIT.
7. **Formatting:** the one-file `ruff format` goes into the Phase 1 PR as its own commit, and is named in `.git-blame-ignore-revs`. A formatting PR merged before Phase 1 would run the old build against the old image path. (Whether `ruff format` gets a PR of its own is an open question in the standards, not a rule.)
8. **The other questions (Q5–Q9, Q11, Q12, Q14–Q16):** as recommended:
   - uv, the ruff fixes and the first tests go in Phase 1;
   - the base image is `python:3.13-slim`;
   - `monitor.py` stays a single file (recorded exception);
   - candidates C1 and C2 are kept;
   - the app is unversioned (the `v*` trigger is dropped);
   - Dependabot runs monthly;
   - Phase 3 gets its own PR;
   - `docker-compose.yml` keeps its name;
   - Phases 1–4 are four PRs, and Phase 5 is n/a.
9. **Standards (c6fdd38):** `PYTHONUNBUFFERED` is in the profile. The "before going public" scan is in the checklist. Six Python questions are open in the standards' open-questions.md.

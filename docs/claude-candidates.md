# Candidate rules

Rules an agent thinks this project should have. **Not binding.** John promotes a candidate into
CLAUDE.md, rewrites it, or deletes it. Append; don't edit others' entries.

Format:

```
## YYYY-MM-DD — <rule, one line>
Why: <what goes wrong without it>
From: <PR or issue>
```

## 2026-11-15 — A new environment variable is added to `docker-compose.yml`'s `environment:` list, not only to `Settings`
Why: compose has no `env_file`, so a variable it doesn't list never reaches the container. The app then silently uses the default, or refuses to boot if the variable is required, even though it is set in the stack environment. The same change also adds it to `.env.example` and to the variables table in `docs/DEPLOYMENT.md`.
From: #15

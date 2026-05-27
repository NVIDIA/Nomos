# Agent Guidance

This directory is the canonical home for AI-facing repository guidance.
Human-facing product and operator docs belong in `docs/`; durable rules for
coding agents belong here.

Start here:

- `architecture.md` - physical design, levelization, and John Lakos-style dependency rules.
- `security.md` - security posture, auth, OPA, secrets, and privileged execution expectations.
- `coding-guidelines.md` - implementation standards, TDD, unit tests, and review discipline.
- `deployment.md` - Docker stack, env files, accounts, and release checks.
- `troubleshooting.md` - known failure modes and fast diagnosis paths.

Agents working in this repository should treat these files as project
constraints. If the codebase and these files disagree, either update the file
as part of the change or call out the mismatch explicitly.

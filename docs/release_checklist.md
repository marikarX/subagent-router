# Release Checklist

## Versioning Policy

- Keep `pyproject.toml` and `package.json` versions aligned.
- Use semantic versioning for public CLI, config schema, provider behavior, and
  Codex integration changes.
- Document config migrations in this file and in `CHANGELOG.md`.

## Pre-Release Checks

- Run `pytest -q`.
- Run `git diff --check`.
- Run `subagent-router doctor --mock`.
- Run a mock stdin/stdout handoff:
  `echo '{"model":"deepseek-chat","stream":false,"input":"hello","tools":[]}' | subagent-router stdio --mock`.
- Verify `subagent-router paths --json`, `subagent-router usage --json`, and
  `subagent-router debug-bundle` succeed.
- Confirm generated caches, logs, local state, and debug bundles are excluded
  from release artifacts.

## Upgrade Notes

- `subagent-router init` preserves user-customized files unless `--force` is
  passed.
- Managed Codex integration files are tracked with
  `.subagent-router-manifest.json`.
- Relative path settings continue to resolve under
  `SUBAGENT_ROUTER_STATE_DIR`.

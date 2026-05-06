# Security Policy

## Reporting a vulnerability

Please do not open public issues for security-sensitive reports.

Send reports to: danat.freedom@gmail.com

Include:
- affected version or commit
- reproduction steps
- expected impact
- relevant logs with secrets removed

## Secret handling

subagent-router is designed to run locally and should not require users to expose provider API keys to third-party services.

Users should never commit:
- API keys
- provider tokens
- `.env` files
- local config containing credentials
- usage logs containing private prompts

## Supported versions

Security fixes are currently applied to the latest public version only while the project is under active development.

# Privacy Notes

subagent-router is intended to run locally as developer infrastructure.

## Data handled by the tool

Depending on configuration, the tool may process:

- prompts sent by a coding agent
- model responses
- provider names
- token usage
- estimated cost
- routing decisions
- local configuration

## Local-first design

The project is designed so routing and usage tracking can run locally. It does not require a hosted control plane.

## Provider traffic

When a cloud model provider is configured, prompts and responses are sent to that provider according to the provider's own API terms and privacy policy.

## Logs

Users should review log settings before using the tool with confidential code or private prompts.

Do not publish logs that may contain:
- source code
- secrets
- API keys
- customer data
- private prompts

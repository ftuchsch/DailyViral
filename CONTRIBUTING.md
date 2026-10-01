# Contributing

DailyViral is a small Python project with offline tests and a separate protected
publishing workflow. Contributions should keep those boundaries clear.

## Development

Create a Python 3.11+ virtual environment and follow the development setup in the
README. Runtime dependencies come from the hash-locked `requirements.txt`; CI
installs its development tools directly at pinned versions. Run the formatting, linting, test,
dependency-audit, and publication checks listed in the README before submitting
a pull request. Tests must use dummy credentials and mocked external services.

Use explicit types, small functions, clear error messages, and docstrings that
explain meaningful constraints. Keep API/storage operations in their modules;
CLI code should orchestrate them. Add regression tests for behavior changes,
especially authentication, URL validation, storage privacy, and cleanup.

Avoid new runtime dependencies when the existing libraries handle the task.
When a dependency is necessary, use a local dependency manifest to regenerate
`requirements.txt`, and review the complete transitive version and hash diff.
Install the runtime lock after development tools so tests use the deployed versions.

## Security-sensitive changes

Never commit `.env`, real account configuration, media, private keys, logs, or
queue databases. Do not include real secrets or signed URLs in tests, issues,
screenshots, or workflow artifacts. See [SECURITY.md](SECURITY.md) for private
reporting and incident response.

Publishing secrets belong only in the protected environment and the final
publishing step. Do not add privileged pull-request triggers, unreviewed actions,
public bucket access, debug environment dumps, or repository write permissions.
Pin actions to full commit hashes. Changes to credential handling, workflow
permissions, network destinations, and storage authorization need owner review.

## Pull requests

Explain the concrete behavior change and why it is needed. Include the validation
performed and any deployment/configuration migration. Keep unrelated refactoring
separate where practical. No live Instagram post is required to contribute.

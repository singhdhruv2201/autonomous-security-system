# Project engineering instructions

## Test-driven development

- Use test-driven development for every behavioral implementation and bug fix.
- Before changing implementation code, write or update a focused unit test for
  the intended behavior. Run it and confirm it fails for the expected reason.
- Implement the smallest change that makes the test pass.
- Refactor with tests passing, then rerun the relevant tests.
- After implementation, review coverage and add any missing unit or regression
  tests for meaningful edge cases and failure behavior before declaring completion.
- For behavior crossing API, database, or service boundaries, add appropriate
  integration tests alongside unit tests; do not rely only on mocked interactions.
- Test observable behavior and invariants rather than duplicating implementation
  logic in assertions.
- Run the relevant tests before completing work. Report what was tested, the
  result, and any checks that could not run. Do not claim unexecuted tests passed.
- Documentation-only edits do not require artificial unit tests. For configuration
  changes, use the relevant validation or smoke checks.

## Current test command

Run `.venv/bin/python -m pytest -q` from the repository root. Follow README.md
to install the locked development dependencies when needed.

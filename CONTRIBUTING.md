# Maintainer and evaluator guide

This is a private evaluation repository, not an open-source contribution program.
Access and modifications are governed by [LICENSE](LICENSE). Awasi's authorized
founders may evaluate it; additional contributors need the owner's permission.

## Local development

Follow the [installation guide](docs/getting-started.md), using Python 3.11 and the
locked development dependencies. `make help` lists the main commands.

```sh
make showcase          # Recorded model, no Slack or paid requests
make test              # Backend tests; Neo4j tests skip unless configured
make test-showcase     # Showcase service tests and JavaScript syntax
make test-ui           # Dashboard JavaScript behavior/layout tests
make check             # The three check groups above
```

The default test command does not discover `showcase/tests`; run `make check`
before submitting changes that affect both modes. For real Neo4j coverage, start
only the needed local database and run `make test-integration`. Tests use isolated
company namespaces; do not reset the demo to prepare test data.

CI uses a disposable Neo4j service, Python 3.11, and Node.js 22. It runs the backend,
showcase, and frontend checks without production credentials, live model calls,
or Slack messages. Live checks require a deliberate, separately budgeted run and
must be reported separately from fake-provider or recorded-output tests.

## Change process

1. Follow [AGENTS.md](AGENTS.md): reproduce behavioral changes with a failing test,
   implement the smallest fix, and rerun the relevant unit and integration checks.
2. Preserve source authentication, graph provenance, approval boundaries, action
   verification, and explicit uncertainty. Do not replace failed verification with
   an assumed success to make the demo look complete.
3. Keep model calls and external message delivery disabled in automated tests.
4. Update the applicable current guide and record meaningful limitations. Preserve
   historical reports under `docs/archive/`; do not rewrite old results as new ones.
5. Review `git diff --check` and `git status` before committing. Use a commit subject
   that describes the result, with a body explaining behavior and validation.

Documentation-only changes need link/command checks, not artificial unit tests.
Do not claim checks passed if they were skipped, interrupted, or not executed.

## Version-control boundaries

Commit source, deliberate synthetic fixtures, tests, documentation, locked
dependencies, and deployment configuration. Do not commit `.env`, API/Slack tokens,
private keys, local databases, model-usage ledgers, generated evaluation reports,
virtual environments, caches, build output, or editor state.

`showcase/recordings.json` is the intentional synthetic walkthrough fixture; label
recorded assessments as recorded. New live reports belong in ignored `data/`
directories. Preserve usage history rather than resetting ledgers to bypass limits.

The `helio_*` packages and Compose names model the synthetic customer and preserve
runtime compatibility. A repository rename does not require renaming them.

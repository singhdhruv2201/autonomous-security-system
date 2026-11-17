# Autonomous Security System

An independent working prototype by **Dhruv Singh**, prepared for evaluation by
Awasi's founders. It demonstrates how a security product can connect company
context to investigation, human decisions, and verified remediation. This is not
an official Awasi release or a production deployment.

**Start here:** [what it does](#what-the-product-does) ·
[run the showcase](#run-the-showcase) ·
[run the full stack](docs/getting-started.md#3-run-the-full-docker-stack-optional) ·
[future scope](#future-scope-not-yet-built)

**Private evaluation repository, not open source.** Awasi's founders have limited
evaluation permission under the [Proprietary Evaluation License](LICENSE).
Repository access is by invitation; other use requires written permission.

## What the product does

The goal is to answer three questions: **who or what can access company data,
where that access creates risk, and what verified action will resolve it?**

The current implementation follows a complete case lifecycle:

1. **Observe:** collect employment, ownership, permissions, app visibility, and
   destination metadata from connected services.
2. **Connect:** build an access graph linking people, accounts, apps, agents,
   grants, data, and external destinations.
3. **Detect:** evaluate deterministic rules against the observed evidence.
4. **Investigate:** let a model inspect evidence through bounded, read-only tools
   and explain the situation, impact, and uncertainty.
5. **Resolve:** apply the configured containment and approval workflow, ask the
   right person in Slack or the showcase, verify the resulting state, and record
   the outcome. Supported actions have Undo paths.

The model investigates and writes explanations. Deterministic code controls
permissions, action selection, approvals, verification, and rollback. A model
failure cannot grant it additional authority or turn an uncertain action into a
verified success.

### What works today

The demo company, **Helio**, has 20 synthetic employees, a CRM, two internal apps,
two Workbench agents, and a local receiver representing an outside destination.
These are working local services, not live Salesforce, Google Workspace, or
other SaaS integrations.

| Scenario | What Awasi detects | What the workflow does |
| --- | --- | --- |
| Orphaned agent | Maya Chen becomes inactive, but her Sales Research Agent retains CRM access. | Pause access, ask Hannah whether to retire it, then require Casey's approval before revoking the grant. |
| Public internal app | Renewal Tracker becomes public while holding confidential customer access. | Ask Luca whether sharing is intentional; make it company-only and verify the public URL no longer opens. Observed anonymous access triggers immediate containment. |
| Unapproved destination | Lead Enrichment sends synthetic customer records to an unapproved receiver. | Pause the agent, ask the approver what to do, and verify connection removal and resumption if chosen. Records already sent cannot be recalled. |

The interfaces expose the access graph, risk paths, evidence, case history,
verified actions, and human decisions. Cases and checkpoints persist in the
Docker stack; the showcase instead gives each browser session fresh temporary data.

The repository also includes **optional CRM activity capture, signed webhook
delivery, durable ingestion, recovery, and processing**. That pipeline is separate
from the main demo's snapshot-based detection. Workbench is currently polled;
the whole product is not an instant webhook-driven monitoring system.

### Two ways to explore it

| | Browser showcase | Full Docker stack |
| --- | --- | --- |
| Best for | A guided product walkthrough | Inspecting the service architecture and persistent workflows |
| Access graph | In-memory projection of normalized service snapshots | Neo4j |
| Services and actions | Real local service handlers, policies, workflows, and playbooks | Separate Directory, CRM, Workbench, outside receiver, security API, and worker services |
| Human decisions | Browser controls; optional Slack | Slack |
| State | Isolated temporary session databases; reset from the UI | Persistent Docker volumes |
| Model | Recorded assessments by default in the quickstart; optional live Anthropic | Live Anthropic when configured; explicit fallback on failure |

Recorded assessments are labeled as recordings, not presented as fresh model
reasoning. The showcase's local graph does not demonstrate Neo4j operation.

## Run the showcase

Accept your private GitHub invitation first. Use Python 3.11 and run from the
repository root. This walkthrough needs no Docker, Slack credentials, or API keys.

```sh
git clone https://github.com/singhdhruv2201/autonomous-security-system.git
cd autonomous-security-system
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
make showcase
```

Open **[http://127.0.0.1:3070](http://127.0.0.1:3070)**, click **Start baseline**,
and run a scenario. Inspect its graph and timeline, then use the owner/approver
controls to complete the case. Use **New baseline** between scenarios. `Ctrl+C`
stops the showcase and removes its temporary session data.

`make showcase` explicitly uses recorded assessments and disables Slack: it does
not spend API credits or send messages. The scenario actions still execute against
real local service handlers with synthetic data.

- [Installation and operation](docs/getting-started.md): complete walkthrough,
  live model/Slack configuration, Docker services, spending limits, and reset.
- [Showcase guide](showcase/README.md): scenarios and browser/Slack decisions.
- [Architecture](docs/architecture.md): service boundaries and evidence flow.
- [Documentation index](docs/README.md): current guides and historical records.

## Future scope (not yet built)

The current result demonstrates three bounded security workflows. It is not yet
a general-purpose autonomous security platform. The next product work would be:

| Area | Remaining scope |
| --- | --- |
| Real integrations | Production connectors for CRM, identity, collaboration, and agent platforms; OAuth installation, scope discovery, rate limits, and provider-specific actions. |
| Broader detection | More access and data-exposure rules, cross-service correlations, and the proposed recurring 15-minute AI review of suspicious activity beyond deterministic rules. |
| Faster prevention | Workbench event delivery and a coherent event-driven graph; selective inline checks before high-risk operations. Today's polling can detect a send only after it happened. |
| Customer identity and isolation | Multi-tenant onboarding, SSO, named operators, production role mappings, and tenant-scoped storage and credentials. The current solo role-switching experience is for demonstration. |
| Production operation | Durable queues and a production database/checkpointer, multiple workers, deployment automation, backups, observability, and load/recovery testing. Current workflows use a single worker and SQLite. |
| Evaluation and trust | Broader model and adversarial evaluations, stronger evidence completeness checks, and measured reliability for connectors, human decisions, and remediation under failure. |
| Cleanup and hardening | Complete the planned removal of legacy optional CRM enforcement, revisit the simulator review backlog, and resolve dashboard availability issues observed under concurrent load. |

The current demo also has explicit limits: it cannot recover data already sent;
activity scanners and response checks must not be described as clean scans when
not configured; and uncertain actions require review. A previous full-suite run
under load was not clean, so this README makes no blanket claim that every
integration check currently passes.

The [known limitations](docs/known-limitations.md) track concrete open issues.
The original [v1 plan](docs/archive/awasi-v1-plan.md) is archived as an implementation
record, not the current setup guide.

## Repository layout

```text
.github/workflows/    Automated verification
src/                 Runtime services and security workflows
showcase/            Guided browser demo, assets, and its tests
  tests/             Showcase HTTP, isolation, and decision coverage
tests/               Backend, integration, and frontend tests
docs/                Current operation and architecture guides
  archive/           Historical contracts and implementation records
compose*.yaml        Local service topology and acceptance overlay
requirements*.lock   Locked runtime and development dependencies
```

Helio is the synthetic customer company; `helio_*` Python package names and the
`helio-simulator` Compose project are intentional and remain stable.
Generated databases, credentials, recordings from new runs, and usage ledgers
stay outside version control. `showcase/recordings.json` is an intentional,
labeled synthetic assessment fixture.

Run `make help` for common commands. [Contributing](CONTRIBUTING.md) documents
maintainer checks and test-driven changes; [SECURITY.md](SECURITY.md) describes
the evaluation boundary and private reporting. See [docs](docs/README.md) for
service-specific guides.

## License and evaluation permission

Copyright © 2026 Dhruv Singh. Original repository material is provided under the
[Awasi Founders Evaluation License](LICENSE), a proprietary, non-open-source
license. Awasi's founders may run and make local modifications solely to evaluate
this prototype. Production use, incorporation into another product, redistribution,
and access for additional evaluators require separate written permission.

GitHub invitations control repository access; the license defines permitted use.
It does not override applicable third-party licenses or rights granted through
[GitHub's Terms of Service](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service).
Contact [Dhruv Singh](https://github.com/singhdhruv2201) for access or additional
permission. Awasi names are used to identify the intended recipient; no affiliation,
endorsement, or trademark license is implied.

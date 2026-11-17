# Documentation

Start with the [project overview](../README.md) and the
[installation guide](getting-started.md). This index separates current operation
from historical development records.

## Review and run

| Guide | Purpose |
| --- | --- |
| [Installation and operation](getting-started.md) | Browser showcase, optional live model/Slack, Docker stack, costs, and reset |
| [Showcase walkthrough](../showcase/README.md) | Scenarios, human decisions, session isolation, and live/recorded labels |
| [Architecture](architecture.md) | Components, data flow, trust boundaries, and repository map |
| [Known limitations](known-limitations.md) | Concrete open issues and validation gaps |

## Service guides

| Guide | Purpose |
| --- | --- |
| [Security backend](backend.md) | Observation, findings, API authorization, and case services |
| [Access graph](graph.md) | Neo4j inventory, normalized sources, and access paths |
| [Dashboard](dashboard.md) | Graph, risk paths, case timelines, and activity views |
| [CRM](crm.md) | Synthetic customers, principals, grants, and source enforcement |
| [Model operations](model-operations.md) | Read tools, bounded investigations, fallback, and accounting |
| [Slack](slack-setup.md) | App setup, decisions, notification delivery, and recovery |
| [Activity capture and delivery](activity-delivery.md) | Optional CRM events and signed webhooks |
| [Activity processing](activity-processing.md) | Durable ingestion, recovery, and Activity view |

## Maintaining the repository

See [CONTRIBUTING.md](../CONTRIBUTING.md), [AGENTS.md](../AGENTS.md),
[SECURITY.md](../SECURITY.md), and the [evaluation license](../LICENSE).

## Historical records

The [archive](archive/README.md) preserves implementation decisions and past
verification results. Those documents are not current installation instructions;
old test totals and phase-status notes must not be read as present-day guarantees.

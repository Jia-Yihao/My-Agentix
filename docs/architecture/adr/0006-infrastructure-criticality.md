# ADR-0006: Infrastructure Criticality and Failure Policy

> 重编号：原与 ADR-0003（declarative-configuration）撞号，按 adr/README 索引顺序改为 0006（2026-09-27）。

## Decision

PostgreSQL and the active vector/graph retrieval backend are critical for
grounded legal answers. Redis checkpointer/cache, external search and
telemetry sinks are optional and must degrade explicitly when unavailable.

## Consequences

- Critical dependencies have bounded timeout, retry classification, health
  checks and fail-closed behavior for missing legal evidence.
- Optional dependencies cannot turn a successful grounded answer into a 500;
  their failure is recorded as degraded telemetry.
- Adding another database requires an owner, backup/restore procedure,
  migration checksum and a documented reason it cannot be replaced by an
  existing dependency.

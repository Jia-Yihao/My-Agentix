# Request Execution Graph and Critical Path

## Standard request path

```text
HTTP request
  -> API key / trace middleware
  -> ChatService request scope
  -> router agent
  -> selected route
      -> general response
      -> KB retrieval (PostgreSQL/Milvus, optional external search)
      -> KG planner -> tool selection -> Neo4j/Text2SQL/GraphRAG
  -> completion validation
  -> response metadata, trajectory and usage metrics
```

## Critical path

The critical path is the shortest path required to return a legally grounded
answer:

1. Authentication, trace creation and request budget initialization.
2. Router decision.
3. One or more selected knowledge retrieval calls.
4. LLM answer generation.
5. Citation and completion validation.
6. Persist response metrics and return the answer.

Every critical-path step has a timeout or request-level budget. A failure must
produce an explicit degraded/error state rather than an ungrounded success.

## Optional path

The following are optional enhancements and must not prevent a grounded
response when they fail:

- Redis checkpointer persistence;
- long-term memory recall;
- reranking;
- external search;
- non-critical trajectory payload capture;
- dashboard/telemetry delivery.

Optional components must remain observable: failures are recorded with the
request `trace_id`, component name and degraded reason.

## Change rule

Adding an agent, tool or data store requires documenting its input/output,
timeout, retry policy, budget impact, failure mode and whether it is critical
or optional. The change is incomplete until the graph and the corresponding
integration test are updated.

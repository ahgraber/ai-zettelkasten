# 008 - Workflow Orchestration

## Status

December 23, 2024 - Accepted

## Context

The AI Zettelkasten system requires orchestration for managing complex workflows including:

- Document ingestion and conversion pipelines
- Long-running parsing and extraction tasks
- Scheduled reprocessing and batch operations
- Retry logic for failed operations
- Coordination between multiple processing stages

The project is self-hosted, personal/internal in scope, and requires a solution that balances operational complexity with workflow durability. Three self-hosted orchestration platforms were evaluated: Temporal.io, Prefect.io, and Windmill.dev.

## Decision

### Selected Approach

**Prefect.io** will be used as the primary workflow orchestration platform for the AI Zettelkasten pipeline.

### Rationale

Prefect offers the best balance of simplicity, observability, and functionality for a personal/internal project prioritizing rapid iteration:

- **Python-native**: Seamless integration with existing Python codebase
- **Low operational overhead**: Simpler deployment and maintenance compared to Temporal
- **Quick setup**: Faster time-to-value for initial implementation
- **Good scheduling**: Built-in cron and event-based triggers
- **Strong observability**: Dashboard and logging for workflow monitoring
- **Iterative development**: Easy to evolve workflows as requirements change

While Prefect has weaker exactly-once guarantees than Temporal, these guarantees are sufficient for this use case where occasional retries are acceptable and idempotent operations are preferred.

### Consequences

#### Positive Impacts

- **Rapid development**: Low learning curve accelerates initial implementation
- **Reduced operational burden**: Fewer components to deploy and monitor
- **Better developer experience**: Pythonic API and good documentation
- **Adequate reliability**: Sufficient durability for document processing workflows
- **Cost-effective**: Lower resource requirements for self-hosting

#### Potential Risks

- **Weaker durability guarantees**: Less robust exactly-once semantics compared to Temporal
- **Scaling limitations**: May encounter performance constraints at very high scale (unlikely for personal project)
- **Backend dependency**: Durability depends on backend database configuration

#### Mitigation Strategies

- **Design for idempotency**: Ensure all workflow tasks can safely retry
- **Service boundary preservation**: Keep FastAPI service layer independent of orchestration to enable future migration
- **Proper backend configuration**: Configure Prefect backend for adequate durability
- **Migration path**: Maintain option to migrate to Temporal if requirements change

### Alternatives Considered

#### Option 1: Temporal.io

**Description**: Event-sourced workflow engine with strong durability guarantees

**Pros**:

- Extremely robust exactly-once workflows
- Excellent handling of long-running operations
- Rich primitives: signals, timers, retries, compensation
- Best-in-class durability for complex state machines
- Strong guarantees for conversions and reprocessing

**Cons**:

- Higher operational complexity (requires server and workers)
- Steeper learning curve with different programming model
- More infrastructure to maintain
- Overkill for current project scope

**Reason for not selecting**: The operational overhead and complexity don't justify the benefits for a personal/internal project. The strong guarantees are valuable but not essential given the nature of document processing workloads where occasional retries are acceptable.

#### Option 2: Windmill.dev

**Description**: UI-driven automation platform with visual workflow building

**Pros**:

- Fast setup for UI-driven automation
- Good for glue tasks and administrative workflows
- Excellent for human-in-the-loop operations
- Low-code/no-code capabilities
- Useful for operator actions (bulk retries, cancellations)

**Cons**:

- Less suited for heavy, durable, long-running pipelines
- More constrained Python integration
- Not designed for core conversion pipeline workloads
- Weaker programmatic workflow definition

**Reason for not selecting**: Better suited as a complementary tool for administrative flows rather than core pipeline orchestration. May be added later for operator-facing workflows.

## Implementation Details

**Initial Setup**:

1. Deploy Prefect server (self-hosted)
2. Configure PostgreSQL backend for workflow state
3. Set up worker pools for task execution
4. Implement core workflows:
   - Document ingestion pipeline
   - Parsing and extraction workflow
   - Reprocessing and batch operations

**Key Workflows to Implement**:

- `ingest_document`: Handle new document addition
- `parse_and_extract`: Run parsing and extraction pipeline
- `reprocess_batch`: Batch reprocessing of existing documents
- `scheduled_cleanup`: Periodic cleanup operations

**Integration Points**:

- Trigger workflows via FastAPI endpoints
- Store workflow state in Prefect backend
- Report status back to application database
- Log to central observability system

**Migration Considerations**:

- Keep orchestration logic separate from core business logic
- Maintain clean service boundaries
- Use dependency injection for orchestration clients
- Design tasks to be orchestration-agnostic where possible

## Related ADRs

- [002-content-parsing.md](002-content-parsing.md): Parsing workflows will be orchestrated by Prefect
- [001-content-archiving.md](001-content-archiving.md): Archiving workflows need orchestration for retries

## Additional Notes

**Future Considerations**:

- If workflow complexity or durability requirements increase significantly, consider migrating to Temporal
- Windmill could be added as a complementary tool for operator-facing administrative workflows
- Monitor Prefect's exactly-once guarantees in production; adjust if issues arise

**References**:

- [Prefect Documentation](https://docs.prefect.io/)
- [Temporal Documentation](https://docs.temporal.io/)
- [Windmill Documentation](https://docs.windmill.dev/)

# ADR 0009: Canonical Agent Memory Platform

## Status
Accepted

## Context
The platform persists raw operational evidence in Postgres. Knowledge and project
profiles previously mixed Neo4j and Postgres ownership. Curated memory and
context assembly need one governed runtime path that:

- works for every project, whatever its workflows do
- supports workflow-agent context assembly and reusable runtime memory
- preserves scope, trust, provenance, and auditability
- does not require dataset-row indexing

## Decision
The platform uses Postgres `memory_records` as the canonical runtime store for
curated knowledge and project memory. The canonical application package lives in
`core/application/memory/` and separates:

- `writer`
- `curator`
- `retrieval`
- `policy`
- `entities`
- `graphs`
- `context assembly`
- `audit`
- `exceptions`

This ADR governs the product's runtime memory and context services. It does not
define contributor or coding-agent workflow and does not require repository work
to query runtime memory before implementation.

Release rules:

- Curated memory and context graph storage are Postgres-backed.
- Neo4j is not a runtime memory or project-profile backend.
- Raw events, workflow runtime messages, agent messages, and jobs remain
  raw evidence stores and are not themselves curated memory.

## Memory Model
Canonical curated memory is stored in:

- `memory_records`
- `memory_entities`
- `memory_entity_aliases`
- `memory_record_entities`
- `memory_edges`

`memory_records` carries scope (`account`, `tenant`, `project`, `workflow`,
`job`, `session`), lifecycle (`candidate`, `active`, `superseded`, `rejected`,
`expired`), provenance (`source_type`, `source_id`, `source_trust`,
`evidence_refs_jsonb`), temporal validity (`valid_from`, `valid_to`,
`supersedes_id`), and retrieval fields (`title`, `summary`, `content`,
`tags_jsonb`, `metadata_jsonb`).

## Source Trust And Human Confirmation
The platform always preserves source trust and evidence refs on curated memory.
The following memory types remain `candidate` unless they come from
`human_approved`, `canonical_doc`, or an explicit promotion path:

- `project_rule`
- `decision`
- `known_issue`
- `dataset_note`
- security-sensitive facts

## Retrieval And Context Assembly
All memory/context consumers call the high-level `HybridRetrievalBackend`
contract through `MemoryRetrievalService`; callers do not manually coordinate
text, vector, and graph indexes. First release exposes exactly one selectable
runtime mode:

- `PostgresUnifiedRetrievalBackend` with repository, knowledge, text, vector,
  and context graph backends co-located in canonical Postgres.

The unified backend preserves the Postgres fast path with one SQL request for a
single hybrid retrieval operation. The retrieval contracts remain
database-agnostic so future adapters can be added in a later task, but external
vector stores are not release-selectable in this release and never become
sources of truth for canonical memory.

All retrieval goes through `MemoryPolicyResolver` before ranking or prompt
assembly. Policy filtering is scope-first and applies:

- account and tenant boundaries
- project/workflow/session/job scope
- visibility
- lifecycle
- source trust
- safety flags

`ContextAssemblyService` is the canonical path for agent-facing context. It
returns deterministic, token-budgeted payloads for:

- `project_profile_context`
- `project_memory`
- `workflow_memory`
- `procedural_memory`
- `docs_memory`
- `graph_context`
- `dataset_context`
- `citations`

Session identity constrains retrieval; the service does not expose a separate
`session_context` output category.

Raw transcripts are not injected by default. Every assembled item includes
citation/provenance metadata.

## Context Graph
`ContextGraphBackend` is the single graph contract, for contextual
knowledge/entity relationships.

## Backend Profiles
Supported release configuration:

- `FS_MEMORY_PROFILE=postgres_unified`
- `FS_MEMORY_REPOSITORY_BACKEND=postgres`
- `FS_MEMORY_KNOWLEDGE_BACKEND=postgres`
- `FS_MEMORY_TEXT_SEARCH_BACKEND=postgres_fts|postgres_trgm`
- `FS_MEMORY_VECTOR_BACKEND=null|postgres_pgvector`
- `FS_CONTEXT_GRAPH_BACKEND=null|postgres_age`
- `FS_PGVECTOR_DSN` for pgvector connection override

Unsupported values fail startup; there is no silent fallback.
Neo4j is not a memory retrieval backend. `apache_age` is reserved as a future backend and is
not selectable in this release. Qdrant and Chroma remain future or community
adapter paths, but are not release-supported or selectable in this release.

## Dataset Extension Point
The release reserves:

- `memory_type = dataset_note`
- dataset-oriented `source_type` values
- `dataset_context`
- generic contextual edges to future dataset references

The release does not implement Dataset Manager functionality for memory dataset-row
indexing; `dataset_context` remains empty. This limitation does not apply to the
existing product datasets UI.

## Migration
Internal project-context assembly and curation resolve through canonical memory
services. The former public KB UI/API, global-search category, MCP tools, and token
scopes are retired. Persisted graph keys `kb_intake` and `kb_context` remain for
checkpoint compatibility, while user-visible copy calls the capability “Project
context”. First-release initialization uses the canonical Postgres baseline;
the release does not expose a Neo4j backfill command.

## Consequences
Positive:

- one canonical runtime path for curated memory
- no duplicate public transport contract over canonical memory
- easier scope, audit, and provenance enforcement
- better support for future dataset-aware workflows

Trade-offs:

- rule-based curation may miss richer semantic extraction without an optional
  extractor implementation
- retrieval supports Postgres text search and optional pgvector search;
  `FS_MEMORY_VECTOR_BACKEND` defaults to `postgres_pgvector`, while explicit
  `null` disables vector retrieval

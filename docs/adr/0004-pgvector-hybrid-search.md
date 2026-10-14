# ADR 0004: pgvector with hybrid search and text-embedding-3-small

- Status: Accepted
- Date: 2026-10-14

## Context

The bot answers tariff and service questions only from a Ukrainian knowledge base, with
citations (FR-3). PostgreSQL is already the system of record. PostgreSQL has no built-in
Ukrainian full-text configuration. Exact product names ("Безлім", "Роумінг Європа") must
be found reliably.

## Decision

Store chunks in PostgreSQL with pgvector (HNSW, cosine) and a `tsvector` column using the
`simple` configuration (GIN index). Search runs vector top-20 and full-text top-20 with
prefix matching, merged with Reciprocal Rank Fusion; top-5 go to the LLM. The "not found"
threshold is the best vector cosine similarity (`min_similarity`, start 0.35), because
RRF scores are rank-based. Embeddings: `text-embedding-3-small`.

## Alternatives considered

- **Qdrant or another vector database.** Another service to run and back up for a corpus
  of a few hundred chunks.
- **text-embedding-3-large.** About 6.5× the price; adopt only if recall@5 on the eval
  set is materially better.
- **Hunspell `uk_UA` dictionary via an ispell template.** Proper stemming, but requires
  dictionary files in the database image; deferred until recall@5 shows the need.

## Consequences

- One database for everything; hybrid search is plain SQL.
- `simple` does no stemming, so word forms differ; the vector branch carries semantics.
- The embedding-model comparison and any hunspell switch will be recorded as follow-up
  ADRs with recall@5 numbers.

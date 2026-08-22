CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE operational_knowledge_chunks (
    reference_document_id TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (
        content_hash ~ '^sha256:[a-f0-9]{64}$'
    ),
    document_version TEXT NOT NULL,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    chunk_text TEXT NOT NULL CHECK (length(btrim(chunk_text)) > 0),
    embedding_model TEXT NOT NULL,
    embedding VECTOR(1536) NOT NULL,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (
        reference_document_id,
        content_hash,
        embedding_model,
        chunk_index
    )
);

CREATE INDEX operational_knowledge_chunks_lookup_idx
    ON operational_knowledge_chunks (
        reference_document_id,
        content_hash,
        embedding_model
    );

CREATE INDEX operational_knowledge_chunks_embedding_hnsw_idx
    ON operational_knowledge_chunks
    USING hnsw (embedding vector_cosine_ops);

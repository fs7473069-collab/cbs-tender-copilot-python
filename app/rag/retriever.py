
import json
import os
import time
from pathlib import Path

import faiss
import numpy as np
import requests


OLLAMA_EMBED_URL = "http://localhost:11434/api/embed"
EMBED_MODEL = "nomic-embed-text"

# Anchored to the project root so the consumer's working directory is irrelevant.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
KB_DIR = PROJECT_ROOT / "local_data" / "cbs_knowledge_base"
INDEX_PATH = KB_DIR / "cbs.index"
METADATA_PATH = KB_DIR / "chunks.json"
INGEST_HINT = "Build it with: python -m app.rag.ingest"
# Opt-in: "|"-separated exact KB source names hidden from retrieval without
# changing the index. Unset or empty keeps every indexed source searchable.
KB_EXCLUDE_SOURCES_ENV = "CBS_KB_EXCLUDE_SOURCES"


def excluded_sources_from_env():
    raw = os.getenv(KB_EXCLUDE_SOURCES_ENV) or ""
    return frozenset(name.strip() for name in raw.split("|") if name.strip())


class RetrievalError(RuntimeError):
    """Evidence retrieval could not run; never equivalent to 'no match'."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

# Process a small number of chunks per Ollama request.
BATCH_SIZE = 8
EMBED_TIMEOUT = 300


def create_embeddings(texts):
    if not texts:
        raise ValueError("No text provided for embedding.")

    all_embeddings = []
    total_batches = (len(texts) + BATCH_SIZE - 1) // BATCH_SIZE

    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start:start + BATCH_SIZE]
        batch_number = start // BATCH_SIZE + 1
        batch_chars = sum(len(text) for text in batch)

        print(
            f"Embedding batch {batch_number}/{total_batches} "
            f"({len(batch)} chunks)...",
            flush=True,
        )

        started = time.perf_counter()
        response = requests.post(
            OLLAMA_EMBED_URL,
            json={
                "model": EMBED_MODEL,
                "input": batch,
            },
            timeout=EMBED_TIMEOUT,
        )
        wall_seconds = time.perf_counter() - started
        response.raise_for_status()

        embeddings = np.asarray(
            response.json()["embeddings"],
            dtype="float32",
        )

        if embeddings.ndim != 2 or len(embeddings) != len(batch):
            raise ValueError("Unexpected embedding response shape.")

        if not np.isfinite(embeddings).all():
            raise ValueError("Embeddings contain invalid values.")

        faiss.normalize_L2(embeddings)
        all_embeddings.append(embeddings)
        print(
            "OLLAMA_EMBED "
            f"model={EMBED_MODEL} batch={batch_number}/{total_batches} "
            f"items={len(batch)} chars={batch_chars} wall_s={wall_seconds:.3f}",
            flush=True,
        )

    return np.vstack(all_embeddings)


def _embed_queries(texts):
    try:
        return create_embeddings(texts)
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise RetrievalError(
            "embedding_failed",
            f"Query embedding failed with model {EMBED_MODEL}: "
            f"{type(exc).__name__}: {str(exc)[:200]}",
        ) from exc


class TenderRetriever:
    def __init__(self, persist=True, excluded_sources=None):
        self.index = None
        self.chunks = []
        self.persist = persist
        self.load_error = None
        self.excluded_sources = (
            excluded_sources_from_env()
            if excluded_sources is None else frozenset(excluded_sources)
        )
        # Cache exact query strings within one process to avoid repeat embeds.
        self._query_embedding_cache = {}

        if self.persist:
            self.load()

    def ingest(self, chunks):
        if not chunks:
            raise ValueError("No chunks provided for ingestion.")

        normalized_chunks = []

        for chunk in chunks:
            text = chunk.get("text", "")

            if not isinstance(text, str) or not text.strip():
                continue

            normalized_chunks.append({
                **chunk,
                "text": text.strip(),
                "source": chunk.get("source", "unknown"),
                "page": chunk.get("page"),
            })

        if not normalized_chunks:
            raise ValueError("No usable text chunks found.")

        embeddings = create_embeddings(
            [chunk["text"] for chunk in normalized_chunks]
        )

        index = faiss.IndexFlatIP(embeddings.shape[1])
        index.add(embeddings)

        self.index = index
        self.chunks = normalized_chunks
        self.load_error = None

        if self.persist:
            self.save()

        return len(self.chunks)

    def save(self):
        if self.index is None:
            raise RuntimeError("There is no index to save.")

        KB_DIR.mkdir(parents=True, exist_ok=True)

        index_tmp = INDEX_PATH.with_suffix(".index.tmp")
        metadata_tmp = METADATA_PATH.with_suffix(".json.tmp")

        faiss.write_index(self.index, str(index_tmp))

        metadata_tmp.write_text(
            json.dumps(self.chunks, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        index_tmp.replace(INDEX_PATH)
        metadata_tmp.replace(METADATA_PATH)

        print(f"Saved FAISS index: {INDEX_PATH}")
        print(f"Saved chunk metadata: {METADATA_PATH}")

    def load(self):
        """Load the persisted index; on failure record why in ``load_error``."""
        self.load_error = None
        missing = [
            path.name for path in (INDEX_PATH, METADATA_PATH)
            if not path.exists()
        ]
        if missing:
            self.load_error = RetrievalError(
                "index_missing",
                f"Knowledge-base index files missing in {KB_DIR}: "
                f"{', '.join(missing)}. {INGEST_HINT}",
            )
            print(f"EVIDENCE_RETRIEVAL load_failed code=index_missing missing={missing}", flush=True)
            return False

        try:
            chunks = json.loads(
                METADATA_PATH.read_text(encoding="utf-8")
            )
            index = faiss.read_index(str(INDEX_PATH))
        except (OSError, ValueError, RuntimeError) as exc:
            self.load_error = RetrievalError(
                "index_unreadable",
                f"Knowledge-base index could not be read "
                f"({type(exc).__name__}). {INGEST_HINT}",
            )
            print(
                "EVIDENCE_RETRIEVAL load_failed code=index_unreadable "
                f"error_type={type(exc).__name__}",
                flush=True,
            )
            return False

        if not isinstance(chunks, list) or index.ntotal != len(chunks):
            self.load_error = RetrievalError(
                "index_metadata_mismatch",
                "FAISS index and metadata counts do not match. "
                f"{INGEST_HINT}",
            )
            print(
                "EVIDENCE_RETRIEVAL load_failed code=index_metadata_mismatch "
                f"index_vectors={index.ntotal}",
                flush=True,
            )
            return False

        self.index = index
        self.chunks = chunks

        print(f"Loaded {len(chunks)} knowledge-base chunks.")
        return True

    def _require_index(self):
        if self.index is None or not self.chunks:
            if self.load_error is not None:
                raise self.load_error
            raise RetrievalError(
                "index_empty",
                f"Knowledge base is empty. Ingest documents first. {INGEST_HINT}",
            )

    def status(self):
        """Non-sensitive availability summary for startup diagnostics."""
        if self.index is not None and self.chunks:
            return {
                "available": True,
                "chunks": len(self.chunks),
                "dimension": int(self.index.d),
                "error_code": None,
            }
        return {
            "available": False,
            "chunks": 0,
            "dimension": None,
            "error_code": getattr(self.load_error, "code", "index_empty"),
        }

    def active_exclusions(self):
        """Configured excluded source names that are present in the index."""
        present = {chunk.get("source") for chunk in self.chunks}
        return sorted(self.excluded_sources & present)

    def _search(self, query_embedding, top_k):
        if query_embedding.shape[-1] != self.index.d:
            raise RetrievalError(
                "embedding_dimension_mismatch",
                f"Query embedding dimension {query_embedding.shape[-1]} does "
                f"not match index dimension {self.index.d}. {INGEST_HINT}",
            )
        excluded = self.excluded_sources
        hidden = (
            sum(1 for chunk in self.chunks if chunk.get("source") in excluded)
            if excluded else 0
        )
        # Over-fetch by the hidden count so exclusions never shrink top_k.
        k = min(top_k + hidden, len(self.chunks))
        scores, indices = self.index.search(query_embedding, k)
        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0:
                continue
            chunk = self.chunks[int(idx)]
            if excluded and chunk.get("source") in excluded:
                continue
            results.append({
                "text": chunk["text"],
                "source": chunk.get("source", "unknown"),
                "page": chunk.get("page"),
                "score": float(score),
                # Position in chunks.json for the current index build.
                "kb_chunk_id": int(idx),
            })
            if len(results) >= top_k:
                break
        return results

    def retrieve(self, question, top_k=3, *, include_metrics=False):
        self._require_index()

        if not isinstance(question, str) or not question.strip():
            raise ValueError("Question cannot be empty.")

        if top_k < 1:
            raise ValueError("top_k must be at least 1.")

        cache_key = question.strip()
        embedding_started = time.perf_counter()
        cached = self._query_embedding_cache.get(cache_key)
        if cached is not None:
            query_embedding = cached
            embedding_seconds = time.perf_counter() - embedding_started
            print(
                "OLLAMA_EMBED cache_hit=1 "
                f"chars={len(cache_key)} wall_s={embedding_seconds:.6f}",
                flush=True,
            )
        else:
            query_embedding = _embed_queries([question])
            self._query_embedding_cache[cache_key] = query_embedding
            embedding_seconds = time.perf_counter() - embedding_started

        search_started = time.perf_counter()
        results = self._search(query_embedding, top_k)
        retrieval_seconds = time.perf_counter() - search_started

        if include_metrics:
            return results, {
                "query_embedding_seconds": round(embedding_seconds, 6),
                "retrieval_seconds": round(retrieval_seconds, 6),
            }
        return results

    def retrieve_many(self, questions, top_k=3, *, include_metrics=False):
        """Retrieve for many queries, batching uncached embeddings."""
        self._require_index()
        if not isinstance(questions, list):
            raise ValueError("Questions must be a list.")
        if top_k < 1:
            raise ValueError("top_k must be at least 1.")

        normalized = []
        for question in questions:
            if not isinstance(question, str) or not question.strip():
                raise ValueError("Question cannot be empty.")
            normalized.append(question.strip())

        pending = []
        pending_seen = set()
        for key in normalized:
            if key in self._query_embedding_cache or key in pending_seen:
                continue
            pending.append(key)
            pending_seen.add(key)

        embedding_seconds = 0.0
        newly_embedded = set()
        if pending:
            started = time.perf_counter()
            vectors = _embed_queries(pending)
            embedding_seconds = time.perf_counter() - started
            if len(vectors) != len(pending):
                raise RetrievalError(
                    "embedding_failed", "Unexpected embedding batch size."
                )
            for key, row in zip(pending, vectors):
                self._query_embedding_cache[key] = np.asarray(
                    row, dtype="float32"
                ).reshape(1, -1)
                newly_embedded.add(key)

        amortized = (
            embedding_seconds / len(newly_embedded)
            if newly_embedded else 0.0
        )
        all_results = []
        all_metrics = []
        for key in normalized:
            query_embedding = self._query_embedding_cache[key]
            search_started = time.perf_counter()
            results = self._search(query_embedding, top_k)
            retrieval_seconds = time.perf_counter() - search_started
            all_results.append(results)
            all_metrics.append({
                "query_embedding_seconds": round(
                    amortized if key in newly_embedded else 0.0, 6
                ),
                "retrieval_seconds": round(retrieval_seconds, 6),
            })

        if include_metrics:
            return all_results, all_metrics
        return all_results

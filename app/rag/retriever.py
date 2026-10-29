
import json
import time
from pathlib import Path

import faiss
import numpy as np
import requests


OLLAMA_EMBED_URL = "http://localhost:11434/api/embed"
EMBED_MODEL = "nomic-embed-text"

KB_DIR = Path("local_data/cbs_knowledge_base")
INDEX_PATH = KB_DIR / "cbs.index"
METADATA_PATH = KB_DIR / "chunks.json"

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


class TenderRetriever:
    def __init__(self, persist=True):
        self.index = None
        self.chunks = []
        self.persist = persist
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
        if not INDEX_PATH.exists() or not METADATA_PATH.exists():
            return False

        chunks = json.loads(
            METADATA_PATH.read_text(encoding="utf-8")
        )
        index = faiss.read_index(str(INDEX_PATH))

        if index.ntotal != len(chunks):
            raise ValueError(
                "FAISS index and metadata counts do not match."
            )

        self.index = index
        self.chunks = chunks

        print(f"Loaded {len(chunks)} knowledge-base chunks.")
        return True

    def _search(self, query_embedding, top_k):
        k = min(top_k, len(self.chunks))
        scores, indices = self.index.search(query_embedding, k)
        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0:
                continue
            chunk = self.chunks[int(idx)]
            results.append({
                "text": chunk["text"],
                "source": chunk.get("source", "unknown"),
                "page": chunk.get("page"),
                "score": float(score),
            })
        return results

    def retrieve(self, question, top_k=3, *, include_metrics=False):
        if self.index is None or not self.chunks:
            raise RuntimeError(
                "Knowledge base is empty. Ingest documents first."
            )

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
            query_embedding = create_embeddings([question])
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
        if self.index is None or not self.chunks:
            raise RuntimeError(
                "Knowledge base is empty. Ingest documents first."
            )
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
            vectors = create_embeddings(pending)
            embedding_seconds = time.perf_counter() - started
            if len(vectors) != len(pending):
                raise ValueError("Unexpected embedding batch size.")
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

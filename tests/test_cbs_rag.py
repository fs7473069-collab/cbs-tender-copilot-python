
from app.rag.retriever import TenderRetriever


sample_chunks = [
    {
        "text": (
            "CBS delivered a fleet management system for a transport "
            "company, including vehicle tracking and reporting."
        ),
        "source": "sample_fleet_project.pdf",
        "page": 3,
    },
    {
        "text": (
            "CBS maintains an information security management "
            "certification for its internal operations."
        ),
        "source": "sample_security_certificate.pdf",
        "page": 1,
    },
    {
        "text": (
            "The company provides software development and "
            "technical support services."
        ),
        "source": "sample_company_profile.docx",
        "page": 2,
    },
]


def main():
    retriever = TenderRetriever(persist=False)

    count = retriever.ingest(sample_chunks)
    print(f"\nIndexed chunks: {count}")

    results = retriever.retrieve(
        "Has CBS delivered a fleet management project?",
        top_k=2,
    )

    print("\nRetrieved evidence:")
    for result in results:
        print(f"Source: {result['source']}")
        print(f"Page: {result['page']}")
        print(f"Similarity: {result['score']:.3f}")
        print(f"Text: {result['text']}\n")


if __name__ == "__main__":
    main()


def test_retrieval_returns_relevant_source(monkeypatch):
    import numpy as np
    import app.rag.retriever as rag_module

    calls = []

    def fake_embeddings(texts):
        # Deterministic vectors for a fast test without Ollama.
        calls.append(list(texts))
        vectors = []
        for text in texts:
            if "fleet management" in text.lower() or "vehicle tracking" in text.lower():
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return np.asarray(vectors, dtype="float32")

    monkeypatch.setattr(rag_module, "create_embeddings", fake_embeddings)

    retriever = TenderRetriever(persist=False)
    retriever.ingest(sample_chunks)

    query = "Has CBS delivered a fleet management project?"
    results = retriever.retrieve(query, top_k=2)
    retriever.retrieve(query, top_k=2)

    assert results
    assert results[0]["source"] == "sample_fleet_project.pdf"
    assert results[0]["page"] == 3
    assert "fleet management" in results[0]["text"].lower()
    # Exact query text should reuse the in-process embedding cache.
    assert sum(1 for batch in calls if batch == [query]) == 1


def test_retrieve_many_batches_embeddings_and_keeps_associations(monkeypatch):
    import numpy as np
    import app.rag.retriever as rag_module

    calls = []

    def fake_embeddings(texts):
        calls.append(list(texts))
        vectors = []
        for text in texts:
            lower = text.lower()
            if "fleet" in lower or "vehicle" in lower:
                vectors.append([1.0, 0.0])
            elif "security" in lower or "certificate" in lower:
                vectors.append([0.0, 1.0])
            else:
                vectors.append([0.5, 0.5])
        return np.asarray(vectors, dtype="float32")

    monkeypatch.setattr(rag_module, "create_embeddings", fake_embeddings)

    retriever = TenderRetriever(persist=False)
    retriever.ingest(sample_chunks)

    queries = [
        "Has CBS delivered a fleet management project?",
        "Does CBS maintain a security certification?",
        "Has CBS delivered a fleet management project?",
    ]
    results = retriever.retrieve_many(queries, top_k=1)

    assert len(results) == 3
    assert results[0][0]["source"] == "sample_fleet_project.pdf"
    assert results[1][0]["source"] == "sample_security_certificate.pdf"
    assert results[2][0]["source"] == "sample_fleet_project.pdf"
    # Ingest once, then one batched call for the two unique queries.
    assert calls[0] == [chunk["text"] for chunk in sample_chunks]
    assert calls[1] == queries[:2]
    assert len(calls) == 2

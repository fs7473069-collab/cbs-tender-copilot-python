import json
import time

from app.llm.ollama_4b_client import extract_requirements


with open("chunks.json", encoding="utf-8") as file:
    chunks = json.load(file)


# Representative chunks:
# 2 = core functional requirements
# 9 = customer requirements
# 12 = pricing/product requirements
# 18 = technical/security requirements
# 21 = API/architecture requirements
test_indexes = [2, 9, 12, 18, 21]

results = []

for index in test_indexes:
    chunk = chunks[index]

    print(f"\nTesting chunk {index + 1} (page {chunk['page']})...")

    start_time = time.time()

    try:
        response = extract_requirements(chunk)
        elapsed = time.time() - start_time

        # Check whether the response is valid JSON
        parsed = json.loads(response)

        requirements = parsed.get("requirements", [])

        results.append({
            "chunk_index": index,
            "page": chunk["page"],
            "valid_json": True,
            "requirement_count": len(requirements),
            "time_seconds": round(elapsed, 2),
            "response": response
        })

        print("Valid JSON: YES")
        print("Requirements:", len(requirements))
        print("Time:", round(elapsed, 2), "seconds")

    except Exception as error:
        elapsed = time.time() - start_time

        results.append({
            "chunk_index": index,
            "page": chunk["page"],
            "valid_json": False,
            "requirement_count": 0,
            "time_seconds": round(elapsed, 2),
            "error": str(error)
        })

        print("Valid JSON: NO")
        print("Error:", error)
        print("Time:", round(elapsed, 2), "seconds")


with open("benchmark_4b_results.json", "w", encoding="utf-8") as file:
    json.dump(
        results,
        file,
        indent=2,
        ensure_ascii=False
    )


print("\n===== BENCHMARK COMPLETE =====")
print("Results saved to benchmark_4b_results.json")
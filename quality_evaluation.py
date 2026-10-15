import json

RESULTS_FILE = "llm_results.json"

ALLOWED_CATEGORIES = {
    "Functional",
    "Technical",
    "Security",
    "Commercial",
    "Non-Functional",
    "Unclear",
}

ALLOWED_CONFIDENCE = {
    "High",
    "Medium",
    "Low",
}

HEADING_WORDS = {
    "reporting",
    "transaction management",
    "remote session control",
    "fault / alert feed",
    "time-tiered rates",
}


with open(RESULTS_FILE, encoding="utf-8") as file:
    results = json.load(file)


# ==========================================================
# Counters
# ==========================================================

total_chunks = 0
valid_json_count = 0
invalid_json_count = 0

total_requirements = 0

empty_text_count = 0
fragment_count = 0
non_requirement_count = 0
invalid_category_count = 0
invalid_mandatory_count = 0
invalid_confidence_count = 0
duplicate_count = 0


# ==========================================================
# Examples
# ==========================================================

fragment_examples = []
non_requirement_examples = []
invalid_category_examples = []
invalid_mandatory_examples = []
invalid_confidence_examples = []
empty_text_examples = []
duplicate_examples = []


# Used for duplicate detection
requirement_texts = []


# ==========================================================
# Process LLM Results
# ==========================================================

for item in results:

    total_chunks += 1

    response = item.get("response")

    if not response:
        invalid_json_count += 1
        continue

    try:
        data = json.loads(response)
        valid_json_count += 1

    except json.JSONDecodeError:
        invalid_json_count += 1
        continue

    requirements = data.get("requirements", [])

    if not isinstance(requirements, list):
        continue

    for requirement in requirements:

        total_requirements += 1

        text = str(requirement.get("text", "")).strip()
        category = requirement.get("category")
        mandatory = requirement.get("mandatory")
        confidence = requirement.get("confidence")

        # --------------------------------------------------
        # Empty requirement text
        # --------------------------------------------------

        if not text:

            empty_text_count += 1

            if len(empty_text_examples) < 20:
                empty_text_examples.append(requirement)

            continue

        # --------------------------------------------------
        # Fragment detection
        # --------------------------------------------------

        words = text.split()

        if (
            len(words) <= 4
            or text.endswith(":")
            or text.endswith(" and")
            or text.endswith(" or")
            or text.endswith(" a")
        ):

            fragment_count += 1

            if len(fragment_examples) < 20:
                fragment_examples.append(text)

        # --------------------------------------------------
        # Possible headings / non-requirements
        # --------------------------------------------------

        if text.lower() in HEADING_WORDS:

            non_requirement_count += 1

            if len(non_requirement_examples) < 20:
                non_requirement_examples.append(text)

        # --------------------------------------------------
        # Category validation
        # --------------------------------------------------

        if category not in ALLOWED_CATEGORIES:

            invalid_category_count += 1

            if len(invalid_category_examples) < 20:
                invalid_category_examples.append(
                    f"{text} -> {category}"
                )

        # --------------------------------------------------
        # Mandatory validation
        # --------------------------------------------------

        if not isinstance(mandatory, bool):

            invalid_mandatory_count += 1

            if len(invalid_mandatory_examples) < 20:
                invalid_mandatory_examples.append(
                    f"{text} -> {mandatory}"
                )

        # --------------------------------------------------
        # Confidence validation
        # --------------------------------------------------

        if confidence not in ALLOWED_CONFIDENCE:

            invalid_confidence_count += 1

            if len(invalid_confidence_examples) < 20:
                invalid_confidence_examples.append(
                    f"{text} -> {confidence}"
                )

        # --------------------------------------------------
        # Duplicate detection
        # --------------------------------------------------

        normalized_text = " ".join(text.lower().split())

        if normalized_text in requirement_texts:

            duplicate_count += 1

            if len(duplicate_examples) < 20:
                duplicate_examples.append(text)

        else:
            requirement_texts.append(normalized_text)


# ==========================================================
# Summary
# ==========================================================

print("\n===== LLM QUALITY EVALUATION =====")

print(f"Total chunks: {total_chunks}")
print(f"Valid JSON responses: {valid_json_count}")
print(f"Invalid JSON responses: {invalid_json_count}")

print(f"\nTotal requirements: {total_requirements}")

print(f"Possible fragments: {fragment_count}")
print(f"Possible non-requirements: {non_requirement_count}")

print(f"Invalid categories: {invalid_category_count}")
print(f"Invalid mandatory values: {invalid_mandatory_count}")
print(f"Invalid confidence values: {invalid_confidence_count}")

print(f"Possible duplicates: {duplicate_count}")


# ==========================================================
# Examples
# ==========================================================

print("\n--- Fragment Examples ---")

for example in fragment_examples:
    print("-", example)


print("\n--- Possible Non-Requirements ---")

for example in non_requirement_examples:
    print("-", example)


print("\n--- Invalid Categories ---")

for example in invalid_category_examples:
    print("-", example)


print("\n--- Invalid Mandatory Values ---")

for example in invalid_mandatory_examples:
    print("-", example)


print("\n--- Invalid Confidence Values ---")

for example in invalid_confidence_examples:
    print("-", example)


print("\n--- Empty Requirement Text ---")

for example in empty_text_examples:
    print("-", example)


print("\n--- Possible Duplicates ---")

for example in duplicate_examples:
    print("-", example)


# ==========================================================
# Final Status
# ==========================================================

schema_errors = (
    invalid_json_count
    + empty_text_count
    + invalid_category_count
    + invalid_mandatory_count
    + invalid_confidence_count
)

print("\n===== FINAL STATUS =====")

if schema_errors == 0:
    print("Structured output validation: PASS")
else:
    print("Structured output validation: REVIEW REQUIRED")

print("\nQuality evaluation completed.")
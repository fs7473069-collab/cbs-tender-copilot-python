import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "qwen3:4b"


def generate_response(prompt):
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL_NAME,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {
                "num_predict": 512
            },
        },
        timeout=120,
    )

    response.raise_for_status()

    return response.json()["response"]


def extract_requirements(chunk):
    prompt = f"""
Extract the requirements from the tender text below.

Return ONLY valid JSON. Do not explain your answer.

Rules:
- Extract only explicit requirements.
- Categories: Functional, Technical, Security, Commercial, Unclear.
- M = mandatory true.
- S or L = mandatory false.
- Do not invent information.
- Ignore document metadata, headings, glossary definitions,
  table headers, configuration data, and explanatory text.
- If there are no requirements, return {{"requirements": []}}.
- Do not include page.
- Confidence must be High, Medium, or Low.

JSON format:
{{
  "requirements": [
    {{
      "text": "...",
      "category": "...",
      "mandatory": true,
      "confidence": "High"
    }}
  ]
}}

Tender text:
{chunk["text"]}
"""

    return generate_response(prompt)
import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "qwen3:8b"

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
        timeout=300,
    )

    response.raise_for_status()

    return response.json()["response"]


def extract_requirements(chunk):
    prompt = f"""
Extract explicit requirements from the tender text below.

Return ONLY valid JSON.
Do not explain.
Do not include markdown.
Do not include any text before or after the JSON.

Rules:
- Extract only explicit requirements.
- Do not invent information.
- Categories: Functional, Technical, Security, Commercial, Unclear.
- M = mandatory true.
- S or L = mandatory false.
- Do not extract document metadata, headings, glossary definitions,
  table headers, or general background information.
- Do not extract incomplete sentences or fragments.
- If there are no requirements, return:
  {{"requirements": []}}
- Do NOT include page in the JSON.
- Confidence must be High, Medium, or Low.

JSON format:
{{
  "requirements": [
    {{
      "text": "...",
      "category": "Functional",
      "mandatory": true,
      "confidence": "High"
    }}
  ]
}}

Tender text:
{chunk["text"]}
"""

    response = generate_response(prompt)

    return response
import os
from openai import OpenAI
from pathlib import Path

# =============================================================================
# CONFIGURATION
# =============================================================================

OPENAI_API_KEY: str = ""

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY environment variable not set")

MODEL_NAME: str = "gpt-5.1"

INPUT_PDF_PATH: Path = Path( "data.pdf" )

OUTPUT_MARKDOWN_PATH: Path = Path("chat_gpt_output.md")

# =============================================================================
# CLIENT INITIALIZATION
# =============================================================================

client: OpenAI = OpenAI(api_key=OPENAI_API_KEY)

# =============================================================================
# STEP 1 — Upload PDF
# =============================================================================

with INPUT_PDF_PATH.open("rb") as pdf_file:
    uploaded_file = client.files.create(
        file=pdf_file,
        purpose="assistants",
    )

file_id: str = uploaded_file.id
print(f"Uploaded PDF file_id={file_id}")

# =============================================================================
# STEP 2 — Send analysis request
# =============================================================================

analysis_prompt: str = """
You are acting as a senior institutional crypto trader and quantitative analyst.

Rules you must follow strictly:
- Base your analysis ONLY on the candle data visible in the provided document
- Do NOT assume indicators that are not explicitly present
- Do NOT invent volume profiles, order book data, or external context
- If information is insufficient, state that clearly

Your task:
- Identify the dominant market regime (trend, range, transition)
- Highlight obvious support and resistance zones
- Comment on volume behavior relative to price movement
- Call out any notable structural patterns (breaks, compressions, expansions)
- Assign a confidence score (1–100%) to your analysis

Tone:
- Professional
- Direct
- No hype
- No retail-style language

Output format:
- Markdown
- Clear section headings
- Concise bullet points
"""

response = client.responses.create(
    model=MODEL_NAME,
    input=[
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": analysis_prompt},
                {"type": "input_file", "file_id": file_id},
            ],
        }
    ],
)

analysis_text: str = response.output_text

# =============================================================================
# STEP 3 — Persist output
# =============================================================================

OUTPUT_MARKDOWN_PATH.write_text(analysis_text, encoding="utf-8")
print(f"Analysis written to {OUTPUT_MARKDOWN_PATH.resolve()}")

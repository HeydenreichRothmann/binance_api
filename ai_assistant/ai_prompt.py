from openai import OpenAI
from pathlib import Path
import re

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm

# =============================================================================
# CONFIGURATION
# =============================================================================


OPENAI_API_KEY: str = ""
MODEL_NAME: str = "gpt-5.1"

INPUT_PDF_PATH: Path = Path("data.pdf")
OUTPUT_MARKDOWN_PATH: Path = Path("chat_gpt_output.md")
OUTPUT_PDF_PATH: Path = Path("chat_gpt_output.pdf")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY not set")

# =============================================================================
# CLIENT
# =============================================================================

client = OpenAI(api_key=OPENAI_API_KEY)

# =============================================================================
# STEP 1 — UPLOAD INPUT PDF
# =============================================================================

with INPUT_PDF_PATH.open("rb") as f:
    uploaded_file = client.files.create(
        file=f,
        purpose="assistants",
    )

file_id: str = uploaded_file.id
print(f"Uploaded PDF: {file_id}")

# =============================================================================
# STEP 2 — ANALYSIS PROMPT
# =============================================================================

analysis_prompt: str = """
You are acting as a senior institutional crypto trader and quantitative analyst.

Your analysis must prioritize the most recent price action.
Earlier candles may be referenced only to explain the current structure.

STRICT CONSTRAINTS:
- Use ONLY the candle data provided
- Do NOT assume indicators, sentiment, order flow, or external context
- If the data does not justify a trade bias, say so explicitly

OBJECTIVE:
Produce a **trade-readiness assessment** that explains *why* a professional would or would not act here.

OUTPUT STRUCTURE:

## Market Context
- Describe the current regime (trend / range / transition)
- 2–3 bullets explaining how recent price action led to this state

## Structure & Levels
- Key support and resistance zones
- What price has respected or failed recently
- Why these levels matter *now*

## Price–Volume Behavior
- How volume is behaving relative to recent price moves
- What this confirms or fails to confirm

## Decision
- Bias: Long / Short / No-trade
- One concise paragraph explaining the decision logic
- One clear invalidation level

## Confidence
- 0–100% confidence score
- If below 60%, bias MUST be “No-trade”

STYLE RULES:
- No storytelling
- No hindsight narration
- Insight over description
- Brevity over completeness
"""

# =============================================================================
# STEP 3 — REQUEST ANALYSIS
# =============================================================================

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
# STEP 4 — WRITE MARKDOWN
# =============================================================================

OUTPUT_MARKDOWN_PATH.write_text(analysis_text, encoding="utf-8")
print(f"Markdown written to {OUTPUT_MARKDOWN_PATH.resolve()}")

# =============================================================================
# STEP 5 — MARKDOWN → STYLED PDF (LIGHT PARSER)
# =============================================================================

def markdown_to_pdf(text: str, output_path: Path) -> None:
    styles = getSampleStyleSheet()

    heading_style = ParagraphStyle(
        "Heading",
        parent=styles["Heading2"],
        spaceAfter=12,
    )

    body_style = ParagraphStyle(
        "Body",
        parent=styles["Normal"],
        spaceAfter=8,
    )

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    elements = []

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line:
            elements.append(Spacer(1, 10))
            continue

        # Headings (##)
        if line.startswith("## "):
            content = line.replace("## ", "", 1)
            elements.append(Paragraph(content, heading_style))
            continue

        # Escape XML
        line = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        # Inline markdown
        line = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"\*(.*?)\*", r"<i>\1</i>", line)

        elements.append(Paragraph(line, body_style))

    doc.build(elements)


markdown_to_pdf(analysis_text, OUTPUT_PDF_PATH)
print(f"PDF written to {OUTPUT_PDF_PATH.resolve()}")

"""Lightweight security features that do not require an external API."""

import re
from io import BytesIO
from typing import Any


PII_PATTERNS = {
    "api_key": re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{20,}\b"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "phone": re.compile(r"(?<!\d)(?:\+?\d[\d ()-]{8,}\d)(?!\d)"),
    "card_number": re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"),
    "password": re.compile(r"(?i)\b(password|passwd|passcode)\s*[:=]\s*\S+"),
}

POISON_PATTERNS = [
    ("instruction_override", re.compile(r"(?i)\b(ignore|disregard|forget)\b.{0,80}\b(previous|prior|system|instructions?|rules?)\b")),
    ("hidden_instruction", re.compile(r"(?i)\b(system prompt|developer message|secret instructions?)\b")),
    ("data_exfiltration", re.compile(r"(?i)\b(reveal|print|dump|exfiltrate|leak)\b.{0,80}\b(secret|key|password|token|credential)\b")),
    ("unsafe_action", re.compile(r"(?i)\b(disable|bypass|evade)\b.{0,50}\b(safety|security|filter|guardrail)\b")),
]


def extract_document_text(filename: str, content: bytes) -> str:
    """Extract text from supported files without saving the upload to disk."""
    suffix = (filename.rsplit(".", 1)[-1].lower() if "." in filename else "")
    if suffix in {"txt", "md", "csv", "json"}:
        return content.decode("utf-8", errors="replace")
    if suffix == "pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(content)).pages)
    if suffix == "docx":
        from docx import Document
        document = Document(BytesIO(content))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    raise ValueError("Unsupported file type. Use PDF, DOCX, TXT, MD, CSV, or JSON.")


def scan_private_data(text: str) -> dict[str, Any]:
    """Find likely secrets/PII and return a redacted copy."""
    redacted = text or ""
    findings = []
    for kind, pattern in PII_PATTERNS.items():
        count = len(pattern.findall(redacted))
        if count:
            findings.append({"type": kind, "count": count})
            redacted = pattern.sub(f"[REDACTED_{kind.upper()}]", redacted)
    return {"detected": bool(findings), "findings": findings, "redacted_text": redacted}


def scan_document(text: str, filename: str = "") -> dict[str, Any]:
    """Detect common prompt-injection instructions embedded in documents."""
    matches = []
    for category, pattern in POISON_PATTERNS:
        if pattern.search(text or ""):
            matches.append(category)
    return {
        "filename": filename or "untitled",
        "poisoning_detected": bool(matches),
        "risk": "high" if len(matches) >= 2 else ("medium" if matches else "low"),
        "categories": matches,
        "message": "Document contains instructions that may attempt to control the assistant." if matches else "No common poisoning patterns detected.",
    }

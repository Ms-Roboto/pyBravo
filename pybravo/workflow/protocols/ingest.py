"""Read protocol text or PDFs without executing or silently rewriting them."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from typing import Any

from pydantic import BaseModel, Field

MAX_TEXT_CHARS = 400_000
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 300


class ProtocolIngestError(ValueError):
    """The source cannot be read completely enough to draft a protocol."""


class SourceParagraph(BaseModel):
    id: str
    text: str
    page: int | None = None
    kind: str = "paragraph"
    section: str = ""


class IngestedProtocol(BaseModel):
    source_id: str
    name: str
    paragraphs: list[SourceParagraph]
    metadata: dict[str, Any] = Field(default_factory=dict)

    def select(self, paragraph_ids: list[str] | None = None) -> "IngestedProtocol":
        """Select supplied passages in source order, retaining their stable IDs."""
        if paragraph_ids is None:
            return self
        selected = set(paragraph_ids)
        unknown = selected - {p.id for p in self.paragraphs}
        if unknown:
            raise ProtocolIngestError("The selection includes paragraph IDs absent from this source.")
        if not selected:
            raise ProtocolIngestError("Select at least one protocol paragraph.")
        return self.model_copy(update={
            "paragraphs": [p for p in self.paragraphs if p.id in selected],
            "metadata": {**self.metadata, "selected_paragraph_ids": [p.id for p in self.paragraphs if p.id in selected]},
        })


def _paragraphs(text: str, source_id: str, *, page: int | None = None) -> list[SourceParagraph]:
    # A line is a useful citation unit for pasted numbered protocols. PDFium
    # wraps lines at layout boundaries, so keep its contiguous lines together.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    chunks = re.split(r"\n\s*\n", normalized) if page is not None else normalized.splitlines()
    return [
        SourceParagraph(id=f"{source_id}-p{page or 0}-{i + 1}", text=part.strip(), page=page)
        for i, part in enumerate(chunks) if part.strip()
    ]


def ingest_text(text: str, source_name: str = "Pasted protocol") -> IngestedProtocol:
    if not isinstance(text, str) or not text.strip():
        raise ProtocolIngestError("Enter protocol text before extracting a plan.")
    if len(text) > MAX_TEXT_CHARS:
        raise ProtocolIngestError(f"Protocol text exceeds {MAX_TEXT_CHARS:,} characters; select a smaller section.")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    source_id = "src-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
    return IngestedProtocol(
        source_id=source_id, name=source_name,
        paragraphs=_paragraphs(normalized, source_id),
        metadata={"parser": "text", "sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest()},
    )


def _pdfium_text(pdf_bytes: bytes, source_id: str, filename: str) -> IngestedProtocol:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise ProtocolIngestError(
            "PDF text extraction needs pypdfium2 (install pybravo[llm]) or a configured PYBRAVO_DOCLING_URL."
        ) from exc
    paragraphs: list[SourceParagraph] = []
    empty_pages: list[int] = []
    count = 0
    try:
        with pdfium.PdfDocument(pdf_bytes) as document:
            count = len(document)
            if count > MAX_PDF_PAGES:
                raise ProtocolIngestError(f"PDF exceeds {MAX_PDF_PAGES} pages; upload the protocol section only.")
            for page_idx in range(count):
                page = document[page_idx]
                try:
                    textpage = page.get_textpage()
                    try:
                        text = textpage.get_text_range()
                    finally:
                        textpage.close()
                finally:
                    page.close()
                if text.strip():
                    paragraphs.extend(_paragraphs(text, source_id, page=page_idx + 1))
                else:
                    empty_pages.append(page_idx + 1)
                if sum(len(p.text) for p in paragraphs) > MAX_TEXT_CHARS:
                    raise ProtocolIngestError("PDF text is too large; upload the relevant protocol section only.")
    except ProtocolIngestError:
        raise
    except Exception as exc:
        raise ProtocolIngestError("The PDF could not be opened; check that it is a valid, unencrypted PDF.") from exc
    if not paragraphs:
        raise ProtocolIngestError(
            "This PDF has no selectable text. Configure PYBRAVO_DOCLING_URL for OCR, "
            "or paste a checked transcription of the protocol."
        )
    return IngestedProtocol(
        source_id=source_id, name=filename, paragraphs=paragraphs,
        metadata={
            "parser": "pdfium", "page_count": count, "pages_without_text": empty_pages,
            "warnings": (["Some pages have no selectable text; verify the source and use OCR if needed."] if empty_pages else []),
            "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        },
    )


async def ingest_pdf(pdf_bytes: bytes, filename: str = "protocol.pdf") -> IngestedProtocol:
    if not pdf_bytes or not pdf_bytes.lstrip().startswith(b"%PDF-"):
        raise ProtocolIngestError("Upload a valid PDF file.")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise ProtocolIngestError("PDF exceeds the 20 MB limit; upload the relevant protocol section only.")
    source_id = "src-" + hashlib.sha256(pdf_bytes).hexdigest()[:20]
    docling_failed = False
    if os.environ.get("PYBRAVO_DOCLING_URL", "").strip():
        from pybravo.workflow.drafter.paper_parser import PaperParserError, parse_pdf_bytes

        try:
            parsed = await parse_pdf_bytes(pdf_bytes, filename=filename)
        except PaperParserError:
            docling_failed = True
        else:
            if parsed.page_count > MAX_PDF_PAGES:
                raise ProtocolIngestError(f"PDF exceeds {MAX_PDF_PAGES} pages; upload the protocol section only.")
            if sum(len(p.text) for p in parsed.paragraphs) > MAX_TEXT_CHARS:
                raise ProtocolIngestError("PDF text is too large; upload the relevant protocol section only.")
            paragraphs = [
                SourceParagraph(id=f"{source_id}-d{i + 1}", text=p.text, page=p.page_no, kind=p.kind, section=p.section)
                for i, p in enumerate(parsed.paragraphs) if p.text.strip()
            ]
            if paragraphs:
                return IngestedProtocol(
                    source_id=source_id, name=filename, paragraphs=paragraphs,
                    metadata={"parser": "docling", "page_count": parsed.page_count, "sha256": hashlib.sha256(pdf_bytes).hexdigest()},
                )
            docling_failed = True
    result = await asyncio.to_thread(_pdfium_text, pdf_bytes, source_id, filename)
    if docling_failed:
        result.metadata.setdefault("warnings", []).append(
            "Docling was unavailable or returned no text; used selectable PDF text. Verify tables and reading order."
        )
    return result

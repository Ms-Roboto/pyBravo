"""Protocol ingestion retains citations and refuses sources it cannot read."""

from __future__ import annotations

import pytest

from pybravo.workflow.protocols import ingest
from pybravo.workflow.protocols.ingest import ProtocolIngestError, ingest_pdf, ingest_text


def _pdf(text: str | None = "Transfer 10 uL from source to destination.") -> bytes:
    """A tiny valid PDF fixture; no external renderer or hardware required."""
    stream = (f"BT /F1 12 Tf 40 200 Td ({text}) Tj ET" if text else "").encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    output = b"%PDF-1.4\n"
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets[1:]:
        output += f"{offset:010d} 00000 n \n".encode()
    output += f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return output


def test_text_ids_stable_and_selection_retains_source_order():
    first = ingest_text("Transfer a small volume.\r\n\r\nCentrifuge for 2 min.")
    second = ingest_text("Transfer a small volume.\n\nCentrifuge for 2 min.", "another filename")
    assert first.source_id == second.source_id
    assert [p.id for p in first.paragraphs] == [p.id for p in second.paragraphs]
    chosen = first.select([first.paragraphs[1].id, first.paragraphs[0].id])
    assert chosen.paragraphs == first.paragraphs
    assert chosen.model_dump()["paragraphs"][1]["page"] is None
    with pytest.raises(ProtocolIngestError, match="absent"):
        first.select(["invented-id"])
    with pytest.raises(ProtocolIngestError, match="at least one"):
        first.select([])


def test_text_empty_and_oversize_fail_without_truncation(monkeypatch):
    with pytest.raises(ProtocolIngestError, match="Enter protocol"):
        ingest_text("  ")
    monkeypatch.setattr(ingest, "MAX_TEXT_CHARS", 8)
    with pytest.raises(ProtocolIngestError, match="exceeds"):
        ingest_text("123456789")


async def test_pdf_selectable_text_fallback_has_page_citations(monkeypatch):
    pytest.importorskip("pypdfium2")
    monkeypatch.delenv("PYBRAVO_DOCLING_URL", raising=False)
    result = await ingest_pdf(_pdf(), "synthetic.pdf")
    again = await ingest_pdf(_pdf(), "renamed.pdf")
    assert result.metadata["parser"] == "pdfium"
    assert result.metadata["page_count"] == 1
    assert result.paragraphs[0].page == 1
    assert "10 uL" in result.paragraphs[0].text
    assert result.paragraphs[0].id == again.paragraphs[0].id


async def test_pdf_no_selectable_text_requires_ocr(monkeypatch):
    pytest.importorskip("pypdfium2")
    monkeypatch.delenv("PYBRAVO_DOCLING_URL", raising=False)
    with pytest.raises(ProtocolIngestError, match="no selectable text"):
        await ingest_pdf(_pdf(None))


async def test_pdf_invalid_header_is_rejected():
    with pytest.raises(ProtocolIngestError, match="valid PDF"):
        await ingest_pdf(b"not a PDF")


async def test_docling_preferred_and_fallback_recorded(monkeypatch):
    from pybravo.workflow.drafter import paper_parser

    monkeypatch.setenv("PYBRAVO_DOCLING_URL", "http://parser.local")

    async def parsed(*args, **kwargs):
        return paper_parser.ParsedPaper(
            markdown="Transfer", page_count=1,
            paragraphs=[paper_parser.ParsedParagraph(paragraph_id="p-1", text="Transfer 10 uL", page_no=1)],
        )

    monkeypatch.setattr(paper_parser, "parse_pdf_bytes", parsed)
    result = await ingest_pdf(_pdf())
    assert result.metadata["parser"] == "docling"
    assert result.paragraphs[0].page == 1

    async def failed(*args, **kwargs):
        raise paper_parser.DoclingServiceError("offline")

    monkeypatch.setattr(paper_parser, "parse_pdf_bytes", failed)
    result = await ingest_pdf(_pdf())
    assert result.metadata["parser"] == "pdfium"
    assert "Docling was unavailable" in result.metadata["warnings"][-1]

"""Deterministic statement PDFs.

Fixed creation date, so identical inputs → identical bytes.
"""

from datetime import UTC, datetime

from fpdf import FPDF

FIXED_DATE = datetime(2026, 1, 1, tzinfo=UTC)


def statement_pdf(title: str, lines: list[str]) -> bytes:
    """Render a one-page statement."""
    pdf = FPDF(format="A4")
    pdf.set_creation_date(FIXED_DATE)
    pdf.set_title(title)
    pdf.set_producer("acme-payer-mock")
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 12, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=11)
    for line in lines:
        pdf.multi_cell(0, 8, line, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())

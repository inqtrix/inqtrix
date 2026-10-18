"""Best-effort chunk→page mapping (the PDF-provenance enabler)."""

from __future__ import annotations

from inqtrix.knowledge.page_mapping import extract_pdf_page_texts, infer_chunk_pages


def test_maps_each_chunk_to_the_page_its_text_appears_on() -> None:
    page_texts = [
        "Artikel 6 Einstufung Hochrisiko KI Systeme als Sicherheitsbauteil.",
        "Anhang III nennt Beschaeftigung und Personalmanagement als Bereiche.",
        "Artikel 43 Konformitaetsbewertung vor dem Inverkehrbringen.",
    ]
    chunks = [
        "Artikel 6: Einstufung der Hochrisiko-KI-Systeme als Sicherheitsbauteil.",
        "Artikel 43 — Konformitaetsbewertung vor dem Inverkehrbringen.",
    ]

    pages = infer_chunk_pages(chunks, page_texts)

    assert pages == [1, 3]


def test_unmapped_chunk_is_none_not_a_guess() -> None:
    page_texts = ["Completely unrelated page content about something else."]
    chunks = ["Ein Satz der auf keiner Seite vorkommt und nichts ueberlappt."]

    assert infer_chunk_pages(chunks, page_texts) == [None]


def test_no_page_texts_yields_all_none() -> None:
    assert infer_chunk_pages(["a", "b", "c"], None) == [None, None, None]
    assert infer_chunk_pages(["a"], []) == [None]


def test_chunks_in_order_resolve_forward_across_repeated_text() -> None:
    # The same heading recurs; the forward cursor keeps later chunks on later
    # pages rather than snapping every match back to the first page.
    page_texts = ["Kapitel Eins Inhalt A.", "Kapitel Zwei Inhalt B.", "Kapitel Drei Inhalt C."]
    chunks = ["Kapitel Eins Inhalt A", "Kapitel Zwei Inhalt B", "Kapitel Drei Inhalt C"]

    assert infer_chunk_pages(chunks, page_texts) == [1, 2, 3]


def test_extract_pdf_page_texts_returns_none_for_non_pdf_bytes() -> None:
    # A non-PDF (no %PDF magic) is not an error — it simply has no pages.
    assert extract_pdf_page_texts(b"This is plain text, not a PDF.") is None
    assert extract_pdf_page_texts(b"") is None

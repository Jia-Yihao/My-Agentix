from langchain_core.documents import Document

from evals.retrieval_matching import doc_matches_gold, first_rank


def test_legal_citation_match_normalizes_article_number_forms():
    doc = Document(page_content="《民法典》第一百四十八条")

    assert doc_matches_gold("《民法典》第148条", doc)


def test_first_rank_returns_first_matching_document():
    docs = [
        Document(page_content="《民法典》第143条"),
        Document(page_content="《民法典》第148条"),
        Document(page_content="《民法典》第148条"),
    ]

    assert first_rank(docs, ["《民法典》第148条"]) == 2

"""
Data crawlers / importers for bootstrapping the knowledge base.

Currently supported sources:
- Wikipedia (via MediaWiki API)
"""

from .wikipedia import fetch_wikipedia_pages, wikipedia_page_to_legal_document

__all__ = ["fetch_wikipedia_pages", "wikipedia_page_to_legal_document"]


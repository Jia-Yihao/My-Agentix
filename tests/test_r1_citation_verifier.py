"""R1-2：引用验证器 v1 测试。

依据：《技术变更与架构演进指南 v2.0》§2.1.5⑥——引用验证器从"可选幻觉检查"
升级为强制 Verify：解析答案引用 → 核验存在性 → 未验证引用显式标注。
验收：伪造/证据外引用被正确标记；证据内引用零误伤。
"""

from __future__ import annotations

from agentix.retrieval.domain.services.citation_verify import (
    extract_statute_citations,
    verify_answer_citations,
)

_HITS = [
    {
        "chunk_id": "c1",
        "source": "中华人民共和国民法典",
        "content": "《中华人民共和国民法典》第五百七十七条 当事人一方不履行合同义务或者履行合同义务不符合约定的，应当承担继续履行、采取补救措施或者赔偿损失等违约责任。",
    },
    {
        "chunk_id": "c2",
        "source": "劳动合同法",
        "content": "第十条 建立劳动关系，应当订立书面劳动合同。",
    },
]


def test_extract_statute_citations_chinese_and_arabic():
    text = "依据《中华人民共和国民法典》第五百七十七条……另见《劳动合同法》第10条。"

    refs = extract_statute_citations(text)

    assert ("中华人民共和国民法典", "五百七十七") in refs
    assert ("劳动合同法", "10") in refs
    assert len(refs) == 2  # 去重保序


def test_extract_returns_empty_for_no_citation():
    assert extract_statute_citations("这里没有法条引用。") == []
    assert extract_statute_citations("") == []


def test_verified_citation_not_annotated():
    answer = "根据《中华人民共和国民法典》第五百七十七条，应承担违约责任。"

    annotated, verified, unverified = verify_answer_citations(answer, _HITS)

    assert annotated == answer  # 原样返回，零误伤
    assert verified == [("中华人民共和国民法典", "五百七十七")]
    assert unverified == []


def test_forged_citation_is_annotated():
    answer = "根据《XX伪造法》第一条，当事人应当赔偿。"

    annotated, verified, unverified = verify_answer_citations(answer, _HITS)

    assert ("XX伪造法", "一") in unverified
    assert "引用核验提示" in annotated
    assert "《XX伪造法》第一条" in annotated


def test_mixed_citations_only_unverified_annotated():
    answer = (
        "依据《中华人民共和国民法典》第五百七十七条承担违约责任；"
        "另外《不存在的法》第二条也有规定。"
    )

    annotated, verified, unverified = verify_answer_citations(answer, _HITS)

    assert len(verified) == 1
    assert len(unverified) == 1
    assert annotated.endswith("《不存在的法》第二条")
    assert annotated.startswith(answer[:20])


def test_empty_hits_no_annotation():
    """无证据路径（拒答/无检索）不标注——没有证据就谈不上与证据不一致。"""
    answer = "根据《民法典》第五百七十七条……"

    annotated, verified, unverified = verify_answer_citations(answer, [])

    assert annotated == answer
    assert verified == []
    assert unverified == []


def test_hit_chunks_shape_supports_verification():
    """hit_chunks_from_docs 补充的 content/source 字段可支撑交叉核验。"""
    from agentix.retrieval.usecases.chat_service import hit_chunks_from_docs

    class _FakeDoc:
        page_content = "《中华人民共和国民法典》第五百七十七条 当事人一方不履行合同义务……"
        metadata = {"chunk_id": "c9", "_retrieval_score": 0.9, "source": "民法典"}

    chunks = hit_chunks_from_docs([_FakeDoc()])

    assert chunks[0]["chunk_id"] == "c9"
    assert "第五百七十七条" in chunks[0]["content"]
    assert chunks[0]["source"] == "民法典"

    answer = "依据《中华人民共和国民法典》第五百七十七条，违约方应承担违约责任。"
    _, verified, unverified = verify_answer_citations(answer, chunks)
    assert verified and not unverified

import json
import re
from typing import Any, Dict, List, Optional

import numpy as np
from langchain_core.prompts import ChatPromptTemplate
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher.prompts import (
    PARAM_EXTRACTION_HUMAN_PROMPT,
    PARAM_EXTRACTION_SYSTEM_PROMPT,
)


class VectorQueryMatcher:
    """使用 TF-IDF 向量化实现的查询匹配器。"""

    def __init__(
        self,
        predefined_cypher_dict: Dict[str, str],
        query_descriptions: Dict[str, str],
        similarity_threshold: float = 0.5,
    ) -> None:
        self.predefined_cypher_dict = predefined_cypher_dict
        self.query_descriptions = query_descriptions
        self.similarity_threshold = similarity_threshold

        self._vectorizer = TfidfVectorizer()
        self._query_vectors = self._compute_query_vectors()

    def _compute_query_vectors(self) -> Dict[str, np.ndarray]:
        keys: List[str] = []
        corpus: List[str] = []
        for query_name in self.predefined_cypher_dict:
            description = self.query_descriptions.get(query_name, "")
            keys.append(query_name)
            corpus.append(f"{query_name} {description}".strip())

        if not corpus:
            return {}

        matrix = self._vectorizer.fit_transform(corpus).toarray()
        return {
            key: np.asarray(vector, dtype=np.float32) for key, vector in zip(keys, matrix)
        }

    def _embed(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, len(self._vectorizer.get_feature_names_out())))
        return self._vectorizer.transform(texts).toarray()

    def match_query(self, user_question: str, top_k: int = 3) -> List[Dict[str, Any]]:
        if not user_question or not self._query_vectors:
            return []

        question_vector = self._embed([user_question])
        if question_vector.size == 0:
            return []
        question_vector = question_vector[0]

        similarities: List[tuple[str, float]] = []
        for query_name, vector in self._query_vectors.items():
            score = cosine_similarity([question_vector], [vector])[0][0]
            similarities.append((query_name, float(score)))

        similarities.sort(key=lambda item: item[1], reverse=True)

        results: List[Dict[str, Any]] = []
        for query_name, score in similarities[:top_k]:
            if score >= self.similarity_threshold:
                results.append(
                    {
                        "query_name": query_name,
                        "similarity": score,
                        "cypher": self.predefined_cypher_dict[query_name],
                    }
                )
        return results

    def extract_parameters(
        self, user_question: str, query_name: str, llm: Any | None = None
    ) -> Dict[str, str]:
        if query_name not in self.predefined_cypher_dict:
            return {}

        cypher_template = self.predefined_cypher_dict[query_name]
        param_names = re.findall(r"\$(\w+)", cypher_template)
        if not param_names:
            return {}

        if llm is not None:
            llm_params = self._extract_parameters_with_llm(
                user_question, param_names, query_name, llm
            )
            if llm_params:
                return llm_params

        return self._extract_parameters_with_rules(user_question, param_names)

    @staticmethod
    def _extract_parameters_with_rules(
        user_question: str, param_names: List[str]
    ) -> Dict[str, str]:
        """基于正则的兜底参数提取（LLM 不可用时使用）。

        参数名与 ``legal_cypher_dict.py`` 保持一致：
        ``$法规名`` / ``$条号`` / ``$案由名`` / ``$机关名`` / ``$关键词`` / ``$类别``。
        """
        params: Dict[str, str] = {}
        for name in param_names:
            if name == "法规名":
                match = re.search(
                    r"《([^《》]{1,30})》"
                    r"|([\u4e00-\u9fa5]{1,12}(?:法典|法|条例|规定|办法))",
                    user_question,
                )
                if match:
                    params[name] = (match.group(1) or match.group(2) or "").strip()
            elif name == "条号":
                match = re.search(
                    r"第\s*([0-9一二三四五六七八九十百千零两]+)\s*条", user_question
                )
                if match:
                    params[name] = match.group(1)
            elif name == "案由名":
                match = re.search(
                    r"(?:案由|纠纷)[：:是为]?\s*([^\s，。,、的]+)", user_question
                )
                if match:
                    params[name] = match.group(1)
            elif name == "机关名":
                match = re.search(
                    r"(全国人民代表大会(?:常务委员会)?|最高人民法院|最高人民检察院|"
                    r"国务院|司法部|公安部|民政部|人力资源和社会保障部)",
                    user_question,
                )
                if match:
                    params[name] = match.group(1)
            elif name == "关键词":
                match = re.search(
                    r"(?:关键词|涉及|关于|有关)[：:是为]?\s*([^\s，。,、]+)",
                    user_question,
                )
                if match:
                    params[name] = match.group(1)
            elif name == "类别":
                match = re.search(
                    r"(人格权|婚姻家庭|继承|物权|合同|侵权责任|劳动争议|公司|证券|"
                    r"知识产权|海事海商|民事|刑事|行政)",
                    user_question,
                )
                if match:
                    params[name] = match.group(1)
        return params

    @staticmethod
    def _extract_parameters_with_llm(
        user_question: str,
        param_names: List[str],
        query_name: str,
        llm: Any,
    ) -> Dict[str, str]:
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", PARAM_EXTRACTION_SYSTEM_PROMPT),
                ("human", PARAM_EXTRACTION_HUMAN_PROMPT),
            ]
        )

        response = llm.invoke(
            prompt.format_prompt(
                user_question=user_question,
                query_name=query_name,
                param_names=", ".join(param_names),
            )
        )
        content = getattr(response, "content", "") or ""
        try:
            match = re.search(r"{.*}", content, re.DOTALL)
            if not match:
                return {}
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return {
                    str(k): str(v)
                    for k, v in parsed.items()
                    if v is not None and str(v).strip()
                }
        except Exception as exc:  # pragma: no cover - defensive logging
            print(f"无法解析LLM响应为JSON: {exc}")
        return {}


def create_vector_query_matcher(
    predefined_cypher_dict: Dict[str, str],
    query_descriptions: Optional[Dict[str, str]] = None,
) -> VectorQueryMatcher:
    descriptions = query_descriptions or {
        key: key.replace("_", " ") for key in predefined_cypher_dict.keys()
    }
    return VectorQueryMatcher(predefined_cypher_dict, descriptions)

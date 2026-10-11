"""
安全 - 输出过滤器 + 幻觉检测

提供多层输出安全防护：
1. PII 脱敏：手机号、身份证、银行卡、邮箱等
2. 有害内容检测：暴力、色情、政治敏感等
3. 幻觉检测：检查回答是否基于检索上下文
4. 合规过滤：敏感词、违禁词过滤

使用方式：
    from agentix.security.output_filter import OutputFilter, HallucinationDetector
    
    # 创建过滤器
    filter = OutputFilter()
    
    # 过滤输出
    result = await filter.filter(answer, context=chunks)
    if result.blocked:
        return "抱歉，我无法回答这个问题"
    
    # 幻觉检测
    detector = HallucinationDetector()
    score = await detector.detect(answer, context)
    if score < 0.7:
        return "抱歉，我无法确认这个信息的准确性"
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# ============================================================
# PII 脱敏
# ============================================================

class PIIMasker:
    """个人敏感信息脱敏器"""
    
    # 正则表达式模式
    PATTERNS = {
        "phone": re.compile(r"1[3-9]\d{9}"),
        "id_card": re.compile(r"[1-9]\d{5}(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx]"),
        "bank_card": re.compile(r"[1-9]\d{15,18}"),
        "email": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
        "ip_address": re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    }
    
    # 替换模板
    REPLACEMENTS = {
        "phone": "1**********",
        "id_card": "*******************",
        "bank_card": "*******************",
        "email": "***@***.com",
        "ip_address": "***.***.***.***",
    }
    
    def mask(self, text: str) -> tuple[str, list[str]]:
        """脱敏处理
        
        Returns:
            (脱敏后的文本, 检测到的敏感信息类型列表)
        """
        detected = []
        result = text
        
        for pii_type, pattern in self.PATTERNS.items():
            if pattern.search(result):
                detected.append(pii_type)
                result = pattern.sub(self.REPLACEMENTS[pii_type], result)
        
        return result, detected


# ============================================================
# 有害内容检测
# ============================================================

class HarmCategory(Enum):
    """有害内容分类"""
    VIOLENCE = "violence"
    SEXUAL = "sexual"
    HATE = "hate"
    SELF_HARM = "self_harm"
    POLITICAL = "political"
    ILLEGAL = "illegal"


class HarmfulContentDetector:
    """有害内容检测器（基于关键词 + 规则）"""
    
    # 关键词库（示例，实际应更完善）
    KEYWORDS = {
        HarmCategory.VIOLENCE: [
            "杀人", "暴力", "武器", "爆炸", "枪击",
        ],
        HarmCategory.SEXUAL: [
            "色情", "裸体", "性交易",
        ],
        HarmCategory.HATE: [
            "歧视", "仇恨", "种族",
        ],
        HarmCategory.SELF_HARM: [
            "自杀", "自残", "割腕",
        ],
        HarmCategory.ILLEGAL: [
            "毒品", "赌博", "洗钱",
        ],
    }
    
    def __init__(self, custom_keywords: dict = None):
        # 深拷贝内层 list：dict.copy() 是浅拷贝，extend 会改写类属性 KEYWORDS
        # 的共享 list，导致自定义词跨实例/跨测试污染。
        self.keywords = {category: list(words) for category, words in self.KEYWORDS.items()}
        if custom_keywords:
            for category, words in custom_keywords.items():
                if category in self.keywords:
                    self.keywords[category].extend(words)
                else:
                    self.keywords[category] = list(words)
    
    def detect(self, text: str) -> tuple[bool, list[HarmCategory], list[str]]:
        """检测有害内容
        
        Returns:
            (是否有害, 检测到的分类列表, 匹配到的关键词列表)
        """
        text_lower = text.lower()
        detected_categories = []
        matched_keywords = []
        
        for category, keywords in self.keywords.items():
            for keyword in keywords:
                if keyword in text_lower:
                    detected_categories.append(category)
                    matched_keywords.append(keyword)
        
        is_harmful = len(detected_categories) > 0
        return is_harmful, list(set(detected_categories)), matched_keywords


# ============================================================
# 幻觉检测
# ============================================================

@dataclass
class HallucinationResult:
    """幻觉检测结果"""
    score: float  # 0-1，越高越可信
    is_grounded: bool  # 是否基于上下文
    unsupported_claims: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


class HallucinationDetector:
    """幻觉检测器
    
    检测策略：
    1. 关键词覆盖率：回答中的关键词是否在上下文中出现
    2. 实体一致性：回答中的实体（人名、地名、数字）是否与上下文一致
    3. 否定检测：回答是否否定了上下文中的事实
    """
    
    def __init__(self, threshold: float = 0.7):
        self.threshold = threshold
        self._number_pattern = re.compile(r"\d+\.?\d*")
        self._entity_patterns = [
            re.compile(r"[《「](.+?)[》」]"),  # 书名号、引号内容
            re.compile(r"\d{4}年"),  # 年份
        ]
    
    async def detect(
        self,
        answer: str,
        context: list[str],
        question: str = "",
    ) -> HallucinationResult:
        """检测回答是否存在幻觉
        
        Args:
            answer: 模型生成的回答
            context: 检索到的上下文片段
            question: 原始问题
            
        Returns:
            HallucinationResult
        """
        if not context:
            return HallucinationResult(
                score=0.5,
                is_grounded=False,
                unsupported_claims=["无上下文可验证"],
            )
        
        context_text = " ".join(context)
        
        # 1. 关键词覆盖率
        keyword_score = self._check_keyword_coverage(answer, context_text)
        
        # 2. 数字一致性
        number_score, number_issues = self._check_number_consistency(answer, context_text)
        
        # 3. 实体一致性
        entity_score, entity_issues = self._check_entity_consistency(answer, context_text)
        
        # 综合评分
        score = (
            keyword_score * 0.4 +
            number_score * 0.3 +
            entity_score * 0.3
        )
        
        unsupported = number_issues + entity_issues
        
        return HallucinationResult(
            score=score,
            is_grounded=score >= self.threshold,
            unsupported_claims=unsupported,
            details={
                "keyword_score": keyword_score,
                "number_score": number_score,
                "entity_score": entity_score,
            },
        )
    
    def _check_keyword_coverage(self, answer: str, context: str) -> float:
        """检查关键词覆盖率"""
        # 提取回答中的关键词（长度 > 2 的词）
        answer_words = set(re.findall(r"[\u4e00-\u9fff]{2,}", answer))
        if not answer_words:
            return 1.0
        
        # 检查关键词是否在上下文中出现
        covered = sum(1 for word in answer_words if word in context)
        return covered / len(answer_words)
    
    def _check_number_consistency(self, answer: str, context: str) -> tuple[float, list[str]]:
        """检查数字一致性"""
        answer_numbers = set(self._number_pattern.findall(answer))
        context_numbers = set(self._number_pattern.findall(context))
        
        if not answer_numbers:
            return 1.0, []
        
        issues = []
        for num in answer_numbers:
            if num not in context_numbers:
                issues.append(f"数字 '{num}' 未在上下文中找到")

        covered = sum(1 for num in answer_numbers if num in context_numbers)
        score = covered / len(answer_numbers)
        
        return score, issues
    
    def _check_entity_consistency(self, answer: str, context: str) -> tuple[float, list[str]]:
        """检查实体一致性"""
        issues = []
        total_entities = 0
        covered_entities = 0
        
        for pattern in self._entity_patterns:
            answer_entities = pattern.findall(answer)
            context_entities = pattern.findall(context)
            
            total_entities += len(answer_entities)
            for entity in answer_entities:
                if entity in context_entities:
                    covered_entities += 1
                else:
                    issues.append(f"实体 '{entity}' 未在上下文中找到")
        
        if total_entities == 0:
            return 1.0, issues
        
        return covered_entities / total_entities, issues


# ============================================================
# 综合输出过滤器
# ============================================================

@dataclass
class FilterResult:
    """过滤结果"""
    blocked: bool  # 是否被拦截
    reason: str  # 拦截原因
    filtered_text: str  # 过滤后的文本
    pii_detected: list[str] = field(default_factory=list)
    harm_detected: list[str] = field(default_factory=list)
    hallucination_score: float = 1.0


class OutputFilter:
    """综合输出过滤器
    
    过滤流程：
    1. PII 脱敏
    2. 有害内容检测
    3. 幻觉检测（可选，需要上下文）
    """
    
    def __init__(
        self,
        enable_pii_masking: bool = True,
        enable_harm_detection: bool = True,
        enable_hallucination_detection: bool = True,
        hallucination_threshold: float = 0.7,
        custom_harm_keywords: dict = None,
    ):
        self.enable_pii_masking = enable_pii_masking
        self.enable_harm_detection = enable_harm_detection
        self.enable_hallucination_detection = enable_hallucination_detection
        
        self.pii_masker = PIIMasker() if enable_pii_masking else None
        self.harm_detector = HarmfulContentDetector(custom_harm_keywords) if enable_harm_detection else None
        self.hallucination_detector = HallucinationDetector(hallucination_threshold) if enable_hallucination_detection else None
    
    async def filter(
        self,
        text: str,
        context: list[str] = None,
        question: str = "",
    ) -> FilterResult:
        """过滤输出文本
        
        Args:
            text: 待过滤的文本
            context: 检索上下文（用于幻觉检测）
            question: 原始问题
            
        Returns:
            FilterResult
        """
        filtered_text = text
        pii_detected = []
        harm_detected = []
        hallucination_score = 1.0
        
        # 1. PII 脱敏
        if self.pii_masker:
            filtered_text, pii_detected = self.pii_masker.mask(filtered_text)
            if pii_detected:
                logger.info("PII detected and masked: %s", pii_detected)
        
        # 2. 有害内容检测
        if self.harm_detector:
            is_harmful, categories, keywords = self.harm_detector.detect(filtered_text)
            if is_harmful:
                harm_detected = [c.value for c in categories]
                logger.warning("Harmful content detected: %s, keywords: %s", harm_detected, keywords)
                return FilterResult(
                    blocked=True,
                    reason=f"检测到有害内容: {', '.join(harm_detected)}",
                    filtered_text="",
                    pii_detected=pii_detected,
                    harm_detected=harm_detected,
                )
        
        # 3. 幻觉检测
        if self.hallucination_detector and context:
            result = await self.hallucination_detector.detect(filtered_text, context, question)
            hallucination_score = result.score
            if not result.is_grounded:
                logger.warning(
                    "Hallucination detected, score=%.2f, issues=%s",
                    result.score, result.unsupported_claims,
                )
                return FilterResult(
                    blocked=True,
                    reason="回答可能包含不准确的信息",
                    filtered_text="",
                    pii_detected=pii_detected,
                    harm_detected=harm_detected,
                    hallucination_score=hallucination_score,
                )
        
        return FilterResult(
            blocked=False,
            reason="",
            filtered_text=filtered_text,
            pii_detected=pii_detected,
            harm_detected=harm_detected,
            hallucination_score=hallucination_score,
        )

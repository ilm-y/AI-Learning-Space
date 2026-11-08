"""Retriever 层：把跨对话的认知数据读进本次运行。"""

from app.retriever.base import (
    KnowledgeQuery,
    KnowledgeResult,
    KnowledgeStatus,
    RelatedConceptResult,
    RelatedConceptStatus,
    RetrievalError,
    Retriever,
)
from app.retriever.concept_detector import ALIASES, detect_concepts
from app.retriever.concept_resolver import (
    INTENT_CHITCHAT,
    INTENT_KNOWLEDGE,
    INTENT_OTHER,
    INTENT_PREREQUISITE,
    INTENT_REVIEW,
    ConceptResolver,
    ResolvedConcept,
    ResolverCallDecision,
    ResolverDecision,
    ResolverStatus,
    classify_intent,
    decide_resolver_call,
)
from app.retriever.fixture import FixtureRetriever
from app.retriever.http_backend import HttpBackendRetriever
from app.retriever.knowledge import KnowledgeSearchService
from app.retriever.routing import FallbackRetriever, build_retriever

__all__ = [
    "ALIASES",
    "INTENT_CHITCHAT",
    "INTENT_KNOWLEDGE",
    "INTENT_OTHER",
    "INTENT_PREREQUISITE",
    "INTENT_REVIEW",
    "ConceptResolver",
    "FallbackRetriever",
    "FixtureRetriever",
    "HttpBackendRetriever",
    "KnowledgeQuery",
    "KnowledgeResult",
    "KnowledgeSearchService",
    "KnowledgeStatus",
    "RelatedConceptResult",
    "RelatedConceptStatus",
    "ResolvedConcept",
    "ResolverCallDecision",
    "ResolverDecision",
    "ResolverStatus",
    "RetrievalError",
    "Retriever",
    "build_retriever",
    "classify_intent",
    "decide_resolver_call",
    "detect_concepts",
]

"""Retriever 层：把跨对话的认知数据读进本次运行。"""

from app.retriever.base import RetrievalError, Retriever
from app.retriever.concept_detector import ALIASES, detect_concepts
from app.retriever.fixture import FixtureRetriever
from app.retriever.http_backend import HttpBackendRetriever
from app.retriever.routing import FallbackRetriever, build_retriever

__all__ = [
    "ALIASES",
    "FallbackRetriever",
    "FixtureRetriever",
    "HttpBackendRetriever",
    "RetrievalError",
    "Retriever",
    "build_retriever",
    "detect_concepts",
]

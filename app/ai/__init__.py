"""LLM-backed implementations of the Stage 12/13 AI contracts (Stage 18).

``LLMQualificationExtractor`` and ``LLMSalesAdvisor`` implement the pipeline's
``QualificationExtractor`` / ``SalesAdvisor``; ``LLMCommercialExtractor`` the commercial
``CommercialExtractor``. They run through ``StructuredLLM`` (strict JSON + schema), check
grounding deterministically, and return the existing contract types only. They are
provider-neutral (any ``LLMTransport``) and composed by the runtime; business packages
never import this package.
"""

from app.ai.commercial import LLMCommercialExtractor
from app.ai.qualification import LLMQualificationExtractor, LLMSalesAdvisor

__all__ = ["LLMCommercialExtractor", "LLMQualificationExtractor", "LLMSalesAdvisor"]

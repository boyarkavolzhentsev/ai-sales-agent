"""Versioned prompts of the Stage 12/13 AI contracts, kept with their adapters (the
contracts themselves live in app.pipeline / app.commercial, which never import app.llm).
Same rules as the Stage 5 prompts: untrusted customer text is data, unknown stays unknown,
the model returns a proposal only and deterministic code decides everything."""

from app.llm import LLMTask, PromptTemplate
from app.llm.prompts import UNTRUSTED_DATA_NOTICE

_RULES = (
    "Use only the supplied sections; never use outside knowledge and never guess. If something "
    "is not stated, leave it out (unknown stays unknown). Every proposal must include `quote`: a "
    "short exact copy of the customer's words that states it; numbers must be copied as written. "
    "Answer with a single JSON object that matches the output schema exactly. Do not add fields. "
    "You cannot take actions: you only return a proposal that deterministic code checks."
)

QUALIFICATION_EXTRACTOR_PROMPT_V1 = PromptTemplate(
    prompt_id="qualification_extractor",
    version="1",
    task=LLMTask.QUALIFICATION_EXTRACTION,
    instructions=(
        "You read one customer email and report qualification facts the customer states about "
        "themselves, only for the listed fields. Give each fact a confidence: HIGH only when stated "
        "explicitly, MEDIUM when clearly implied, LOW otherwise. Known facts are shown for context; "
        "report a different value only if the customer states it. List fields the customer did not "
        f"answer in missing_fields.\n{UNTRUSTED_DATA_NOTICE}\n{_RULES}"
    ),
)

COMMERCIAL_EXTRACTOR_PROMPT_V1 = PromptTemplate(
    prompt_id="commercial_extractor",
    version="1",
    task=LLMTask.COMMERCIAL_EXTRACTION,
    instructions=(
        "You read one customer email about a commercial proposal and report what the CUSTOMER asks "
        "for or says: requested terms (price, discount, payment term, dates, SLA, legal terms, ...), "
        "objections, scope changes, and whether their words accept or decline the proposal "
        "(acceptance_quote / decline_quote, else null). Report requests as requests: never decide, "
        "approve, price or counter-offer anything, and never infer a value the customer did not "
        f"write.\n{UNTRUSTED_DATA_NOTICE}\n{_RULES}"
    ),
)

SALES_ADVISOR_PROMPT_V1 = PromptTemplate(
    prompt_id="sales_advisor",
    version="1",
    task=LLMTask.SALES_ADVICE,
    instructions=(
        "You suggest the next sales step for one lead from the structured facts given. Your "
        "recommendation is advisory: an operator and the stage policy decide. Base reasons only on the "
        "given facts; do not invent capabilities, prices, case studies or commitments. evidence_ids must "
        f"be empty (no evidence is supplied).\n{UNTRUSTED_DATA_NOTICE}\n"
        "Answer with a single JSON object that matches the output schema exactly. Do not add fields."
    ),
)

AI_PROMPTS = (QUALIFICATION_EXTRACTOR_PROMPT_V1, COMMERCIAL_EXTRACTOR_PROMPT_V1, SALES_ADVISOR_PROMPT_V1)

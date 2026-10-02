# AI Sales Agent

A sales automation and operator-assistance system for inbound and outbound email workflows, RAG-grounded sales conversations, prospect outreach, follow-ups, lead handling and Telegram operator control.

## Current status

Implemented through Stage 20 (deployment readiness): Gmail, Telegram operator control,
OpenAI/Anthropic/Gemini LLMs, OpenAI/Gemini embeddings with semantic retrieval over approved
local knowledge, and one-shot runtime commands (`python -m app.runtime <command>`). Every
outbound email needs an operator's approval. See [docs/deployment.md](docs/deployment.md) to
deploy and [docs/integrations.md](docs/integrations.md) for providers.

## V1 scope

- Inbound sales email handling
- Outbound prospecting
- Personalized promotional outreach
- RAG-grounded replies
- Lead intent/stage classification
- Operator escalation when knowledge is insufficient
- Follow-up workflows
- Campaign/statistics visibility
- Telegram operator control

## Core safety/product rule

The agent must not fabricate company, product or commercial facts.
When the knowledge base does not contain sufficient reliable information,
the system should escalate to the operator instead of inventing an answer.

## Repository layout

```
app/              Application code (runtime commands: python -m app.runtime --help)
tests/            Tests
knowledge_base/   Approved source knowledge for RAG grounding (see knowledge_base/README.md)
.env.example      Environment variable placeholders
requirements.txt  Python dependencies (Python 3.13)
Dockerfile        Production image (one-shot commands; data on the /data volume)
```

## Configuration

`.env.example` lists every variable. The application reads the process environment only; never
commit `.env`, tokens or credential files.

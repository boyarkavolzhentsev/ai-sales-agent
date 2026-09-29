# AI Sales Agent

A sales automation and operator-assistance system for inbound and outbound email workflows, RAG-grounded sales conversations, prospect outreach, follow-ups, lead handling and Telegram operator control.

## Current status

**Architecture/bootstrap phase.** This repository currently contains only the project skeleton. None of the components listed below are implemented yet.

## Intended V1 scope

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
app/              Application code (to be added per implementation stage)
tests/            Tests
knowledge_base/   Approved source knowledge for RAG grounding (see knowledge_base/README.md)
.env.example      Environment variable placeholders
requirements.txt  Python dependencies (added per implementation stage)
```

## Configuration

Copy `.env.example` to `.env` and fill in values locally. Never commit `.env`.

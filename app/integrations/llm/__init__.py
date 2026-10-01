"""Live LLM providers (Stage 18): OpenAI, Anthropic and Gemini adapters implementing the
existing ``LLMTransport`` over direct HTTPS. Imported only when an LLM provider is
selected; business packages never import this package (enforced by tests)."""

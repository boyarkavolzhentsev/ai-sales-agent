"""Live embeddings providers (Stage 19): OpenAI and Gemini adapters implementing the
provider-neutral ``EmbeddingTransport`` over direct HTTPS. Imported only when an embeddings
provider is selected; business packages never import this package (enforced by tests).
Anthropic offers no first-party embeddings API, so there is no Anthropic adapter."""

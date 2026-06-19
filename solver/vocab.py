"""Canonical vocabulary for the AI topic season.

Two assets:
- VOCABULARY: the list of canonical tags to match candidates against
- CANONICAL_MAP: variant -> canonical lookup, applied as the last step

Refresh periodically by clustering observed tags from other miners and
picking each cluster's centroid as the canonical form.
"""

VOCABULARY: list[str] = [
    # Companies / labs
    "openai", "anthropic", "google", "deepmind", "meta", "microsoft",
    "nvidia", "amd", "apple", "xai", "mistral", "cohere", "perplexity",
    "stability", "hugging face", "huggingface",
    # Products / models
    "gpt", "chatgpt", "claude", "gemini", "llama", "grok", "mistral",
    "deepseek", "qwen", "phi", "command", "sora", "dall-e", "midjourney",
    # Concepts
    "ai", "agi", "asi", "llm", "model", "transformer", "neural network",
    "embedding", "attention", "fine-tuning", "rag", "agent", "reasoning",
    "alignment", "safety", "interpretability", "scaling", "inference",
    "training", "pretraining", "rlhf", "context window", "tokens",
    # Hardware / infra
    "gpu", "tpu", "datacenter", "compute", "h100", "h200", "b200",
    # Use cases
    "coding", "chatbot", "assistant", "search", "image generation",
    "video generation", "voice", "robotics", "autonomous", "self-driving",
    # Events / actions
    "release", "launch", "announcement", "update", "benchmark",
    "research", "paper", "funding", "acquisition", "partnership",
    # Ecosystem
    "open source", "api", "platform", "framework", "library",
    "tensorflow", "pytorch", "jax", "cuda",
    # Money / business
    "ipo", "valuation", "raise", "round", "investor",
    # General tech context
    "tech", "ml", "data", "cloud", "saas", "startup", "enterprise",
]

CANONICAL_MAP: dict[str, str] = {
    # Anthropic family
    "anthropic ai": "anthropic",
    "anthropic inc": "anthropic",
    "anthropic company": "anthropic",
    "the company anthropic": "anthropic",
    "claude ai": "claude",
    "claude 4.7": "claude",
    "claude 4.6": "claude",
    "claude 4.5": "claude",
    "claude opus": "claude",
    "claude sonnet": "claude",
    "claude haiku": "claude",
    "anthropic claude": "claude",
    # OpenAI family
    "open ai": "openai",
    "openai inc": "openai",
    "chat gpt": "chatgpt",
    "gpt-4": "gpt",
    "gpt-4o": "gpt",
    "gpt-4o-mini": "gpt",
    "gpt-5": "gpt",
    "gpt 4": "gpt",
    "gpt 5": "gpt",
    "openai gpt": "gpt",
    # Google family
    "google ai": "google",
    "google deepmind": "deepmind",
    "gemini pro": "gemini",
    "gemini flash": "gemini",
    "gemini 2.0": "gemini",
    "gemini 2.5": "gemini",
    # Meta family
    "meta ai": "meta",
    "llama 3": "llama",
    "llama 3.3": "llama",
    "llama-3": "llama",
    "llama-3.3": "llama",
    # Concepts
    "artificial intelligence": "ai",
    "artificial general intelligence": "agi",
    "artificial super intelligence": "asi",
    "large language model": "llm",
    "large language models": "llm",
    "language model": "llm",
    "ml model": "model",
    "ai model": "model",
    "neural net": "neural network",
    "transformers": "transformer",
    "transformer model": "transformer",
    "fine tuning": "fine-tuning",
    "finetuning": "fine-tuning",
    "retrieval augmented generation": "rag",
    "ai agent": "agent",
    "agents": "agent",
    "reasoning model": "reasoning",
    # Hardware
    "graphics processing unit": "gpu",
    "graphics card": "gpu",
    "nvidia gpu": "gpu",
    "tensor processing unit": "tpu",
    # Misc surface forms
    "machine learning": "ml",
    "deep learning": "ml",
    "huggingface": "hugging face",
    "open-source": "open source",
    "opensource": "open source",
    "ai release": "release",
    "model release": "release",
    "new release": "release",
    "ai announcement": "announcement",
    "ai launch": "launch",
    "research paper": "paper",
    "ai paper": "paper",
}


def canonicalize(tag: str) -> str:
    """Map a tag to its canonical form via the lookup table."""
    return CANONICAL_MAP.get(tag, tag)

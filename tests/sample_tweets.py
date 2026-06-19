"""Synthetic tweets that mirror what the SN101 task server hands out."""

SAMPLE_TWEETS = [
    # 1. Direct topic — vocab cache should crush this.
    "Anthropic released Claude 4.7 today with stronger coding and search.",
    # 2. Multi-entity — should still find canonical tags.
    "OpenAI announced GPT-5 with multimodal reasoning, taking on Gemini and Llama.",
    # 3. Hardware / infra topic.
    "Nvidia's new B200 GPUs are powering the latest LLM training runs at scale.",
    # 4. AI safety / alignment topic.
    "New paper from DeepMind on AI alignment, scaling laws, and interpretability.",
    # 5. Business / funding topic.
    "Mistral raises a fresh round at a multi-billion valuation, chasing OpenAI.",
    # 6. Pure agent topic.
    "AI agents using RAG and tool use are becoming the default deployment pattern.",
    # 7. Off-topic noise — should still produce something safe.
    "I had a great cup of coffee this morning, perfect for the cold weather.",
    # 8. Repeat of #1 to verify exact-cache.
    "Anthropic released Claude 4.7 today with stronger coding and search.",
]

import os
import yaml


def load_config(path="config.yaml"):
    with open(path) as f:
        cfg = yaml.safe_load(f)

    cfg.setdefault("github", {})
    cfg.setdefault("ai", {})

    cfg["github"]["token"] = os.getenv("SCANNER_GITHUB_TOKEN") or cfg["github"].get("token", "")

    # Collect Groq keys: GROQ_API_KEY_1, GROQ_API_KEY_2, ...
    groq_keys = []
    i = 1
    while True:
        key = os.getenv(f"GROQ_API_KEY_{i}")
        if not key:
            break
        groq_keys.append(key)
        i += 1
    if not groq_keys:
        single = os.getenv("GROQ_API_KEY") or cfg["ai"].get("groq_api_key", "")
        if single:
            groq_keys.append(single)

    # Collect Gemini keys: GEMINI_API_KEY_1, GEMINI_API_KEY_2, ...
    gemini_keys = []
    i = 1
    while True:
        key = os.getenv(f"GEMINI_API_KEY_{i}")
        if not key:
            break
        gemini_keys.append(key)
        i += 1
    if not gemini_keys:
        single = os.getenv("GEMINI_API_KEY") or cfg["ai"].get("gemini_api_key", "")
        if single:
            gemini_keys.append(single)

    provider = cfg["ai"].get("provider", "groq").lower()
    if provider == "groq" and not groq_keys:
        raise SystemExit("No Groq API keys found — set GROQ_API_KEY_1 or GROQ_API_KEY")
    if provider == "gemini" and not gemini_keys:
        raise SystemExit("No Gemini API keys found — set GEMINI_API_KEY_1 or GEMINI_API_KEY")

    cfg["ai"]["groq_keys"] = groq_keys
    cfg["ai"]["gemini_keys"] = gemini_keys
    return cfg

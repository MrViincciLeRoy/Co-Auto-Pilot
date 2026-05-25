import re
import json
import logging
from src.groq_client import AIClient, PayloadTooLargeError, AllKeysDead, GROQ_MAX_PROMPT_CHARS
from src.github_utils import build_code_block

log = logging.getLogger(__name__)


def _truncate_prompt(code_block: str, limit: int) -> str:
    if len(code_block) <= limit:
        return code_block
    truncated = code_block[:limit]
    last_nl = truncated.rfind("\n")
    if last_nl > limit * 0.8:
        truncated = truncated[:last_nl]
    return truncated + "\n\n# ... context trimmed to fit model limit"


def analyze_repo(ai: AIClient, repo_name: str, files: dict) -> dict:
    code_block = build_code_block(files)
    prompt_limit = GROQ_MAX_PROMPT_CHARS - 500

    if len(code_block) > prompt_limit:
        log.warning(f"        context too large ({len(code_block):,} chars) — trimming to {prompt_limit:,}")
        code_block = _truncate_prompt(code_block, prompt_limit)

    log.info(f"        sending {len(code_block):,} chars (~{len(code_block) // 2:,} tokens est.) to {ai.provider}")

    prompt = f"""You are analyzing a GitHub repository named "{repo_name}".

{code_block}

Return ONLY valid JSON — no markdown fences, no preamble — shaped exactly like:
{{
  "description": "One sentence max 200 chars describing what this project does",
  "readme": "Full markdown README content"
}}

The README must include: project name, what it does, key features, tech stack, installation, usage, and required env vars if any.
IMPORTANT: In the readme value, escape all backslashes as \\\\ and do not use raw backslashes."""

    try:
        raw = ai.generate(prompt).strip()
    except (PayloadTooLargeError, AllKeysDead):
        raise
    except Exception as e:
        log.error(f"        AI error: {e}")
        return {}

    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        cleaned = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', raw)
        return json.loads(cleaned)

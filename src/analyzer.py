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


def _fix_readme(text: str) -> str:
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")
    if "\\t" in text:
        text = text.replace("\\t", "\t")
    return text.strip()


def _parse_response(raw: str) -> dict:
    if not raw:
        raise ValueError("AI returned empty response")

    # Strip markdown fences if present
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

    if not raw:
        raise ValueError("AI returned empty response after stripping fences")

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        cleaned = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', raw)
        try:
            result = json.loads(cleaned)
        except json.JSONDecodeError as e:
            log.warning(f"        raw response (first 500 chars): {raw[:500]}")
            raise e

    if "readme" in result:
        result["readme"] = _fix_readme(result["readme"])

    return result


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
  "readme": "Full markdown README content here"
}}

Rules:
- The readme value must be a valid JSON string with real newlines escaped as \\n
- Use proper markdown: # headings, ## sections, ``` code blocks, bullet lists with -
- Include: project name, what it does, key features, tech stack, installation, usage, env vars
- Do not include the outer JSON structure inside the readme value itself"""

    try:
        raw = ai.generate(prompt).strip()
    except (PayloadTooLargeError, AllKeysDead):
        raise
    except Exception as e:
        log.error(f"        AI error: {e}")
        return {}

    try:
        return _parse_response(raw)
    except Exception as e:
        log.error(f"        Failed to parse AI response: {e}")
        return {}

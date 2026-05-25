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


def _extract_via_regex(raw: str) -> dict:
    """
    Fallback parser when json.loads fails.
    Pulls description and readme directly using regex so unescaped
    characters inside the readme value don't break the whole parse.
    """
    result = {}

    desc_match = re.search(r'"description"\s*:\s*"(.*?)"(?=\s*,|\s*})', raw, re.DOTALL)
    if desc_match:
        result["description"] = desc_match.group(1).strip()

    # Find where "readme" value starts and grab everything to the end of the JSON blob
    readme_match = re.search(r'"readme"\s*:\s*"(.*)', raw, re.DOTALL)
    if readme_match:
        readme_raw = readme_match.group(1)
        # Strip trailing JSON close — walk back from end to find the last unescaped "
        # that closes the readme value
        cleaned = []
        i = 0
        while i < len(readme_raw):
            ch = readme_raw[i]
            if ch == "\\" and i + 1 < len(readme_raw):
                next_ch = readme_raw[i + 1]
                if next_ch == "n":
                    cleaned.append("\n")
                elif next_ch == "t":
                    cleaned.append("\t")
                elif next_ch == '"':
                    cleaned.append('"')
                elif next_ch == "\\":
                    cleaned.append("\\")
                else:
                    cleaned.append(next_ch)
                i += 2
                continue
            # Unescaped quote = end of JSON string value
            if ch == '"':
                break
            cleaned.append(ch)
            i += 1
        result["readme"] = "".join(cleaned).strip()

    return result


def _parse_response(raw: str) -> dict:
    if not raw:
        raise ValueError("AI returned empty response")

    # Strip markdown fences
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

    if not raw:
        raise ValueError("AI returned empty response after stripping fences")

    # Try clean JSON parse first
    try:
        result = json.loads(raw)
        if "readme" in result:
            result["readme"] = _fix_readme(result["readme"])
        return result
    except json.JSONDecodeError:
        pass

    # Try fixing unescaped backslashes
    try:
        cleaned = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', raw)
        result = json.loads(cleaned)
        if "readme" in result:
            result["readme"] = _fix_readme(result["readme"])
        return result
    except json.JSONDecodeError:
        pass

    # Fallback: regex extraction — handles unterminated strings / unescaped backticks
    log.warning("        JSON parse failed — falling back to regex extraction")
    result = _extract_via_regex(raw)

    if not result.get("description") and not result.get("readme"):
        log.warning(f"        raw response (first 500 chars): {raw[:500]}")
        raise ValueError("Could not extract any content from AI response")

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
- The readme value must be a valid JSON string
- Escape ALL double quotes inside the readme as \\"
- Escape ALL backslashes as \\\\
- Use \\n for newlines — do NOT use literal newlines inside the JSON string value
- Use proper markdown: # headings, ## sections, bullet lists with -
- For code blocks use \\n```\\n instead of literal backtick fences
- Include: project name, what it does, key features, tech stack, installation, usage, env vars"""

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

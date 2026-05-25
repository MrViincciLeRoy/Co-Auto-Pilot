import os
import json
import time
import logging
import argparse
import yaml
from datetime import datetime, timezone, timedelta
from github import Github, GithubException

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".html", ".htm", ".vue", ".svelte",
    ".go", ".rs", ".rb", ".java",
    ".c", ".cpp", ".h", ".hpp",
    ".sh", ".bash", ".zsh",
    ".sql", ".graphql",
    ".php", ".swift", ".kt", ".scala",
    ".tf", ".hcl", ".toml",
    ".yaml", ".yml",
}

SKIP_EXTENSIONS = {
    ".ipynb", ".lock", ".map", ".min.js", ".min.css",
    ".pyc", ".wasm",
}

IGNORE_DIRS = {
    "node_modules", "venv", ".venv", "env", "__pycache__",
    ".git", "dist", "build", "migrations", ".next", "vendor",
    "coverage", ".nyc_output", "static", "media", "assets",
    ".idea", ".vscode", "target", "out", ".gradle",
}

PRIORITY_FILES = {
    "main.py", "app.py", "run.py", "server.py", "manage.py",
    "index.js", "index.ts", "app.js", "app.ts", "server.js",
    "index.html", "main.go", "main.rs", "main.java",
}

# Groq's free tier context limit — stay comfortably under it
GROQ_MAX_PROMPT_CHARS = 28_000


def load_config(path="config.yaml"):
    with open(path) as f:
        cfg = yaml.safe_load(f)

    cfg.setdefault("github", {})
    cfg.setdefault("ai", {})

    cfg["github"]["token"]      = os.getenv("SCANNER_GITHUB_TOKEN") or cfg["github"].get("token", "")
    cfg["ai"]["gemini_api_key"] = os.getenv("GEMINI_API_KEY") or cfg["ai"].get("gemini_api_key", "")

    # Collect Groq keys from GROQ_API_KEY_1, GROQ_API_KEY_2, ... (no upper limit)
    # Falls back to plain GROQ_API_KEY for single-key setups
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

    cfg["ai"]["groq_keys"] = groq_keys
    return cfg


class AIClient:
    def __init__(self, cfg):
        self.provider = cfg["ai"]["provider"].lower()

        if self.provider == "gemini":
            import google.generativeai as genai
            genai.configure(api_key=cfg["ai"]["gemini_api_key"])
            model_name = cfg["ai"].get("gemini_model", "gemini-1.5-flash")
            self.model = genai.GenerativeModel(model_name)
            log.info(f"AI: Gemini · {model_name}")

        elif self.provider == "groq":
            from groq import Groq
            keys = cfg["ai"].get("groq_keys", [])
            if not keys:
                raise SystemExit("No Groq API keys found. Set GROQ_API_KEY_1 (and optionally _2, _3, ...) in secrets.")

            self.model_name  = cfg["ai"].get("groq_model", "llama-3.3-70b-versatile")
            rpm               = cfg["ai"].get("groq_rpm", 25)
            self._groq_delay  = 60.0 / rpm
            self._key_index   = 0

            # Build one Groq client per key
            self._clients = [Groq(api_key=k) for k in keys]
            log.info(f"AI: Groq · {self.model_name} · {len(keys)} key(s) · {rpm} RPM")

        else:
            raise ValueError(f"Unknown AI provider '{self.provider}' — use gemini or groq")

    def _next_groq_client(self):
        client = self._clients[self._key_index % len(self._clients)]
        self._key_index += 1
        return client

    def generate(self, prompt: str) -> str:
        if self.provider == "gemini":
            resp = self.model.generate_content(prompt)
            return resp.text

        elif self.provider == "groq":
            max_retries = len(self._clients) * 2  # give each key at least 2 chances
            for attempt in range(max_retries):
                client = self._next_groq_client()
                try:
                    resp = client.chat.completions.create(
                        model=self.model_name,
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=2000,
                        temperature=0.3,
                    )
                    time.sleep(self._groq_delay)
                    return resp.choices[0].message.content

                except Exception as e:
                    err = str(e).lower()
                    is_payload_too_large = "413" in err or "payload too large" in err or "request too large" in err
                    is_rate_limit        = "429" in err or "rate_limit" in err or "too many" in err

                    if is_payload_too_large:
                        # Rotating keys won't help — the prompt itself is too big.
                        # Caller must truncate and retry with a shorter prompt.
                        raise PayloadTooLargeError("Prompt too large for Groq (413)")

                    if is_rate_limit:
                        wait = 8 * (2 ** (attempt % 4))   # 8 → 16 → 32 → 64, then reset
                        log.warning(
                            f"        rate limit on key {(self._key_index - 1) % len(self._clients) + 1} "
                            f"— rotating + waiting {wait}s (attempt {attempt + 1}/{max_retries})"
                        )
                        time.sleep(wait)
                        continue

                    raise  # anything else: bubble up immediately

            raise RuntimeError(f"Groq failed after {max_retries} attempts")


class PayloadTooLargeError(Exception):
    pass


def is_inactive(repo, inactive_days: int):
    try:
        commits = repo.get_commits()
        if commits.totalCount == 0:
            return None, None
        dt = commits[0].commit.committer.date
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(days=inactive_days)
        return dt < cutoff, dt
    except GithubException:
        return None, None


def _is_stub_readme(content: str, repo_name: str) -> bool:
    stripped = content.strip()
    words = stripped.split()
    if len(words) >= 30:
        return False

    def normalise(s):
        return s.lower().replace("-", "").replace("_", "").replace(" ", "")

    lines = [l.strip() for l in stripped.splitlines() if l.strip()]
    repo_norm = normalise(repo_name)

    if len(lines) <= 1:
        return True
    all_short = all(len(l) < 60 for l in lines)
    any_matches_repo = any(repo_norm in normalise(l) for l in lines)
    if all_short and any_matches_repo:
        return True
    if all(l.startswith("#") for l in lines):
        return True
    return False


def get_readme_state(repo):
    """
    Returns:
      ('missing', None)   — no README file at all
      ('empty', sha)      — README exists but blank/whitespace
      ('stub', sha)       — README has content but it's just a title/repo name
      ('ok', sha)         — README exists and has real content
    """
    for name in ("README.md", "readme.md", "Readme.md"):
        try:
            f = repo.get_contents(name)
            content = f.decoded_content.decode("utf-8", errors="ignore").strip()
            if content == "":
                return "empty", f.sha
            if _is_stub_readme(content, repo.name):
                return "stub", f.sha
            return "ok", f.sha
        except GithubException:
            pass
    return "missing", None


def collect_files(repo, cfg_scanner: dict):
    max_file_chars  = cfg_scanner.get("max_file_chars", 2500)
    max_total_chars = cfg_scanner.get("max_total_chars", 50_000)
    max_file_kb     = cfg_scanner.get("max_file_size_kb", 60)
    max_files       = cfg_scanner.get("max_files", 40)

    files  = {"priority": [], "normal": []}
    total  = 0
    skipped_large = 0

    def _walk(path=""):
        nonlocal total, skipped_large

        if total >= max_total_chars:
            return
        file_count = len(files["priority"]) + len(files["normal"])
        if file_count >= max_files:
            return

        try:
            contents = repo.get_contents(path)
        except GithubException:
            return

        for item in contents:
            if total >= max_total_chars:
                break
            file_count = len(files["priority"]) + len(files["normal"])
            if file_count >= max_files:
                break

            if item.type == "dir":
                if item.name not in IGNORE_DIRS and not item.name.startswith("."):
                    _walk(item.path)

            elif item.type == "file":
                name = item.name
                ext  = os.path.splitext(name)[1].lower()

                if ext in SKIP_EXTENSIONS:
                    continue

                if item.size > max_file_kb * 1024:
                    skipped_large += 1
                    log.debug(f"  skip large file: {item.path} ({item.size // 1024}KB)")
                    continue

                if ext in CODE_EXTENSIONS:
                    try:
                        content = item.decoded_content.decode("utf-8", errors="ignore")
                        if len(content) > max_file_chars:
                            content = content[:max_file_chars] + "\n# ... truncated"
                        entry = (item.path, content)
                        if name in PRIORITY_FILES:
                            files["priority"].append(entry)
                        else:
                            files["normal"].append(entry)
                        total += len(content)
                    except Exception:
                        pass

    _walk()

    if skipped_large:
        log.info(f"        skipped {skipped_large} oversized file(s)")

    return files, total


def build_code_block(files: dict) -> str:
    ordered = files["priority"] + files["normal"]
    return "\n\n".join(f"### {path}\n```\n{content}\n```" for path, content in ordered)


def _truncate_prompt(code_block: str, limit: int) -> str:
    """Hard-trim the code block so the full prompt fits under the Groq char limit."""
    if len(code_block) <= limit:
        return code_block
    truncated = code_block[:limit]
    # Cut at last clean line boundary to avoid mid-line truncation
    last_nl = truncated.rfind("\n")
    if last_nl > limit * 0.8:
        truncated = truncated[:last_nl]
    return truncated + "\n\n# ... context trimmed to fit model limit"


def analyze_repo(ai: AIClient, repo_name: str, files: dict) -> dict:
    code_block = build_code_block(files)

    # Reserve ~500 chars for the prompt wrapper itself
    prompt_limit = GROQ_MAX_PROMPT_CHARS - 500
    if len(code_block) > prompt_limit:
        log.warning(f"        context too large ({len(code_block):,} chars) — trimming to {prompt_limit:,}")
        code_block = _truncate_prompt(code_block, prompt_limit)

    log.info(f"        sending {len(code_block):,} chars to {ai.provider}")

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
    except PayloadTooLargeError:
        # Shouldn't happen after pre-truncation, but handle defensively
        log.error(f"        payload still too large after trimming — skipping")
        return {}

    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

    # Fix common JSON escape issues from model output before parsing
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Try cleaning up bad escape sequences
        import re
        cleaned = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', raw)
        return json.loads(cleaned)


def process_repo(repo, ai: AIClient, cfg: dict, force_recent=False) -> bool:
    s             = cfg.get("scanner", {})
    dry_run       = s.get("dry_run", False)
    overwrite     = s.get("overwrite_readme", False)
    inactive_days = s.get("inactive_days", 30)

    if not force_recent:
        inactive, last_dt = is_inactive(repo, inactive_days)

        if inactive is None:
            log.info(f"  SKIP  {repo.name:<35}  empty repo (no commits)")
            return False

        if not inactive:
            log.info(f"  SKIP  {repo.name:<35}  active ({last_dt.strftime('%Y-%m-%d')})")
            return False

        readme_state, readme_sha = get_readme_state(repo)
        needs_desc   = not repo.description or readme_state == "stub"
        needs_readme = overwrite or readme_state in ("missing", "empty", "stub")

        if not needs_desc and not needs_readme:
            log.info(f"  SKIP  {repo.name:<35}  already documented")
            return False

        reasons = []
        if not repo.description:
            reasons.append("no description")
        if readme_state == "stub":
            reasons.append("stub README")
        log.info(f"  SCAN  {repo.name}  (last commit {last_dt.strftime('%Y-%m-%d')}"
                 + (f" · {', '.join(reasons)}" if reasons else "") + ")")
    else:
        inactive, last_dt = is_inactive(repo, inactive_days)
        readme_state, readme_sha = get_readme_state(repo)
        needs_desc   = not repo.description or readme_state == "stub"
        needs_readme = readme_state in ("missing", "empty", "stub")

        if not needs_desc and not needs_readme:
            log.info(f"  SKIP  {repo.name:<35}  already documented (recent)")
            return False

        reasons = []
        if not repo.description:
            reasons.append("no description")
        if readme_state == "missing":
            reasons.append("no README")
        elif readme_state == "empty":
            reasons.append("empty README")
        elif readme_state == "stub":
            reasons.append("stub README")

        log.info(f"  SCAN  {repo.name}  (recent · {', '.join(reasons)})")

    files, total = collect_files(repo, s)
    count = len(files["priority"]) + len(files["normal"])

    if count == 0:
        log.info(f"        no processable code files — skipping")
        return False

    log.info(f"        {count} file(s) · {total:,} chars")

    try:
        result = analyze_repo(ai, repo.name, files)
    except json.JSONDecodeError as e:
        log.error(f"        AI returned invalid JSON: {e}")
        return False
    except Exception as e:
        log.error(f"        AI error: {e}")
        return False

    if not result:
        return False

    desc   = result.get("description", "")[:255]
    readme = result.get("readme", "")

    if not desc and not readme:
        log.warning(f"        AI returned empty result — skipping")
        return False

    updated = False

    if dry_run:
        log.info(f"        [DRY RUN] desc   → {desc}")
        log.info(f"        [DRY RUN] readme → {readme[:120]}…")
        return True

    if needs_desc and desc:
        try:
            repo.edit(description=desc)
            log.info(f"        ✓ description set")
            updated = True
        except Exception as e:
            log.error(f"        description failed: {e}")

    if needs_readme and readme:
        try:
            if readme_state in ("empty", "stub") and readme_sha:
                commit_msg = (
                    "docs: fill empty README via repo-scanner"
                    if readme_state == "empty"
                    else "docs: rewrite stub README via repo-scanner"
                )
                repo.update_file("README.md", commit_msg, readme, readme_sha)
                log.info(f"        ✓ README {'filled' if readme_state == 'empty' else 'rewritten'} (was {readme_state})")
                updated = True
            elif readme_state == "missing":
                repo.create_file("README.md", "docs: add README via repo-scanner", readme)
                log.info(f"        ✓ README created")
                updated = True
            elif overwrite and readme_sha:
                repo.update_file("README.md", "docs: update README via repo-scanner", readme, readme_sha)
                log.info(f"        ✓ README updated")
                updated = True
        except Exception as e:
            log.error(f"        README failed: {e}")

    time.sleep(1.5)
    return updated


def run_scan(cfg: dict):
    log.info("=" * 55)
    log.info("Repo scanner — starting")

    gh_cfg = cfg.get("github", {})
    token  = gh_cfg.get("token", "")
    if not token:
        raise SystemExit("No GitHub token — set SCANNER_GITHUB_TOKEN env var or config.yaml")

    from github import Auth
    gh       = Github(auth=Auth.Token(token))
    ai       = AIClient(cfg)
    username = gh_cfg.get("username", "").strip()
    if username and username != "your_github_username":
        user = gh.get_user(username)
    else:
        user = gh.get_user()

    repos         = list(user.get_repos(type="owner"))
    skip_forks    = cfg.get("scanner", {}).get("skip_forks", True)
    inactive_days = cfg.get("scanner", {}).get("inactive_days", 30)

    log.info(f"Repos: {len(repos)}  skip_forks={skip_forks}")

    # ── Pass 1: inactive repos ────────────────────────────────────────────────
    log.info("-" * 55)
    log.info("Pass 1 — inactive repos (30+ days)")

    inactive_updated = 0
    for repo in repos:
        if skip_forks and repo.fork:
            log.info(f"  SKIP  {repo.name:<35}  forked")
            continue
        try:
            if process_repo(repo, ai, cfg, force_recent=False):
                inactive_updated += 1
        except Exception as e:
            log.error(f"  ERROR  {repo.name}: {e}")

    log.info(f"Pass 1 done — {inactive_updated} repo(s) updated")

    # ── Pass 2: recent repos (only if Pass 1 did nothing) ────────────────────
    if inactive_updated > 0:
        log.info("-" * 55)
        log.info(f"Pass 2 — SKIPPED (Pass 1 updated {inactive_updated} repo(s))")
        log.info("Scan complete.")
        return

    log.info("-" * 55)
    log.info("Pass 2 — recent repos (oldest-first)")

    cutoff = datetime.now(timezone.utc) - timedelta(days=inactive_days)
    recent = []
    for repo in repos:
        if skip_forks and repo.fork:
            continue
        try:
            _, last_dt = is_inactive(repo, inactive_days)
            if last_dt is None:
                continue
            if last_dt >= cutoff:
                recent.append((last_dt, repo))
        except Exception as e:
            log.error(f"  ERROR  {repo.name}: {e}")

    recent.sort(key=lambda x: x[0])
    log.info(f"Recent repos to check: {len(recent)}")

    recent_updated = 0
    for last_dt, repo in recent:
        try:
            if process_repo(repo, ai, cfg, force_recent=True):
                recent_updated += 1
        except Exception as e:
            log.error(f"  ERROR  {repo.name}: {e}")

    log.info(f"Pass 2 done — {recent_updated} repo(s) updated")
    log.info("Scan complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    run_scan(load_config(args.config))

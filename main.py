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

# ── File filtering ────────────────────────────────────────────────────────────

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

# Always skip these regardless of size — notebooks, minified, lock files, maps
SKIP_EXTENSIONS = {
    ".ipynb",       # Colab / Jupyter — JSON blobs, can be 10MB+
    ".lock",        # yarn.lock, poetry.lock, Pipfile.lock
    ".map",         # JS source maps
    ".min.js",      # minified JS
    ".min.css",     # minified CSS
    ".pyc",         # compiled Python
    ".wasm",        # WebAssembly
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


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path="config.yaml"):
    with open(path) as f:
        cfg = yaml.safe_load(f)

    # Env vars take priority — safe for GitHub Actions secrets
    cfg.setdefault("github", {})
    cfg.setdefault("ai", {})

    cfg["github"]["token"]      = os.getenv("SCANNER_GITHUB_TOKEN") or cfg["github"].get("token", "")
    cfg["ai"]["groq_api_key"]   = os.getenv("GROQ_API_KEY")         or cfg["ai"].get("groq_api_key", "")
    cfg["ai"]["gemini_api_key"] = os.getenv("GEMINI_API_KEY")        or cfg["ai"].get("gemini_api_key", "")

    return cfg


# ── AI client ─────────────────────────────────────────────────────────────────

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
            self.groq = Groq(api_key=cfg["ai"]["groq_api_key"])
            self.model_name = cfg["ai"].get("groq_model", "llama-3.3-70b-versatile")
            log.info(f"AI: Groq · {self.model_name}")

        else:
            raise ValueError(f"Unknown AI provider '{self.provider}' — use gemini or groq")

    def generate(self, prompt: str) -> str:
        if self.provider == "gemini":
            resp = self.model.generate_content(prompt)
            return resp.text

        elif self.provider == "groq":
            resp = self.groq.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2000,
                temperature=0.3,
            )
            return resp.choices[0].message.content


# ── Repo state checks ─────────────────────────────────────────────────────────

def repo_status(repo):
    """
    Returns one of:
      'empty'     — repo exists but has zero commits / no default branch
      'active'    — last commit is within inactive_days
      'inactive'  — last commit older than inactive_days  ← we process these
    """
    try:
        branch = repo.default_branch
        if not branch:
            return "empty"
        commits = repo.get_commits()
        last = commits.totalCount   # triggers the actual API call
        if last == 0:
            return "empty"
        dt = commits[0].commit.committer.date
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except GithubException:
        return "empty"


def is_inactive(repo, inactive_days: int):
    """Returns (True, last_date) if inactive, (False, last_date) if active, (None, None) if empty."""
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


def has_readme(repo):
    for name in ("README.md", "readme.md", "Readme.md"):
        try:
            repo.get_contents(name)
            return True
        except GithubException:
            pass
    return False


# ── File collection with hard limits ─────────────────────────────────────────

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

                # Hard skip list
                if ext in SKIP_EXTENSIONS:
                    continue

                # Size guard — skip files over the KB limit
                if item.size > max_file_kb * 1024:
                    skipped_large += 1
                    log.debug(f"  skip large file: {item.path} ({item.size // 1024}KB)")
                    continue

                if ext in CODE_EXTENSIONS:
                    try:
                        content = item.decoded_content.decode("utf-8", errors="ignore")
                        # Trim to per-file char cap
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


# ── AI analysis ───────────────────────────────────────────────────────────────

def analyze_repo(ai: AIClient, repo_name: str, files: dict) -> dict:
    code_block = build_code_block(files)
    total_chars = len(code_block)
    log.info(f"        sending {total_chars:,} chars to {ai.provider}")

    prompt = f"""You are analyzing a GitHub repository named "{repo_name}".

{code_block}

Return ONLY valid JSON — no markdown fences, no preamble — shaped exactly like:
{{
  "description": "One sentence max 200 chars describing what this project does",
  "readme": "Full markdown README content"
}}

The README must include: project name, what it does, key features, tech stack, installation, usage, and required env vars if any."""

    raw = ai.generate(prompt).strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw
    return json.loads(raw)


# ── Per-repo logic ────────────────────────────────────────────────────────────

def process_repo(repo, ai: AIClient, cfg: dict):
    s             = cfg.get("scanner", {})
    dry_run       = s.get("dry_run", False)
    overwrite     = s.get("overwrite_readme", False)
    inactive_days = s.get("inactive_days", 30)

    # ── Empty repo ──
    inactive, last_dt = is_inactive(repo, inactive_days)

    if inactive is None:
        log.info(f"  SKIP  {repo.name:<35}  empty repo (no commits)")
        return

    # ── Active repo ──
    if not inactive:
        log.info(f"  SKIP  {repo.name:<35}  active ({last_dt.strftime('%Y-%m-%d')})")
        return

    # ── Already documented ──
    needs_desc   = not repo.description
    needs_readme = overwrite or not has_readme(repo)

    if not needs_desc and not needs_readme:
        log.info(f"  SKIP  {repo.name:<35}  already documented")
        return

    log.info(f"  SCAN  {repo.name}  (last commit {last_dt.strftime('%Y-%m-%d')})")

    files, total = collect_files(repo, s)
    count = len(files["priority"]) + len(files["normal"])

    if count == 0:
        log.info(f"        no processable code files — skipping")
        return

    log.info(f"        {count} file(s) · {total:,} chars")

    try:
        result = analyze_repo(ai, repo.name, files)
    except json.JSONDecodeError as e:
        log.error(f"        AI returned invalid JSON: {e}")
        return
    except Exception as e:
        log.error(f"        AI error: {e}")
        return

    desc   = result.get("description", "")[:255]
    readme = result.get("readme", "")

    if not desc and not readme:
        log.warning(f"        AI returned empty result — skipping")
        return

    if dry_run:
        log.info(f"        [DRY RUN] desc   → {desc}")
        log.info(f"        [DRY RUN] readme → {readme[:120]}…")
        return

    if needs_desc and desc:
        try:
            repo.edit(description=desc)
            log.info(f"        ✓ description set")
        except Exception as e:
            log.error(f"        description failed: {e}")

    if needs_readme and readme:
        try:
            sha = None
            for name in ("README.md", "readme.md", "Readme.md"):
                try:
                    sha = repo.get_contents(name).sha
                    break
                except GithubException:
                    pass

            if sha and overwrite:
                repo.update_file("README.md", "docs: update README via repo-scanner", readme, sha)
                log.info(f"        ✓ README updated")
            elif not sha:
                repo.create_file("README.md", "docs: add README via repo-scanner", readme)
                log.info(f"        ✓ README created")
        except Exception as e:
            log.error(f"        README failed: {e}")

    time.sleep(1.5)


# ── Full scan ─────────────────────────────────────────────────────────────────

def run_scan(cfg: dict):
    log.info("=" * 55)
    log.info("Repo scanner — starting")

    gh_cfg = cfg.get("github", {})
    token  = gh_cfg.get("token", "")
    if not token:
        raise SystemExit("No GitHub token — set SCANNER_GITHUB_TOKEN env var or config.yaml")

    gh       = Github(token)
    ai       = AIClient(cfg)
    username = gh_cfg.get("username")
    user     = gh.get_user(username) if username else gh.get_user()
    repos    = list(user.get_repos(type="owner"))

    skip_forks = cfg.get("scanner", {}).get("skip_forks", True)
    log.info(f"Repos: {len(repos)}  skip_forks={skip_forks}")

    for repo in repos:
        if skip_forks and repo.fork:
            continue
        try:
            process_repo(repo, ai, cfg)
        except Exception as e:
            log.error(f"  ERROR  {repo.name}: {e}")

    log.info("Scan complete.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    run_scan(load_config(args.config))

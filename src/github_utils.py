import os
import logging
from datetime import datetime, timezone, timedelta
from github import GithubException

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

SKIP_EXTENSIONS = {".ipynb", ".lock", ".map", ".min.js", ".min.css", ".pyc", ".wasm"}

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
    max_file_chars = cfg_scanner.get("max_file_chars", 2500)
    max_total_chars = cfg_scanner.get("max_total_chars", 14_000)
    max_file_kb = cfg_scanner.get("max_file_size_kb", 60)
    max_files = cfg_scanner.get("max_files", 40)

    files = {"priority": [], "normal": []}
    total = 0
    skipped_large = 0

    def _walk(path=""):
        nonlocal total, skipped_large
        if total >= max_total_chars:
            return
        if len(files["priority"]) + len(files["normal"]) >= max_files:
            return
        try:
            contents = repo.get_contents(path)
        except GithubException:
            return

        for item in contents:
            if total >= max_total_chars:
                break
            if len(files["priority"]) + len(files["normal"]) >= max_files:
                break
            if item.type == "dir":
                if item.name not in IGNORE_DIRS and not item.name.startswith("."):
                    _walk(item.path)
            elif item.type == "file":
                ext = os.path.splitext(item.name)[1].lower()
                if ext in SKIP_EXTENSIONS:
                    continue
                if item.size > max_file_kb * 1024:
                    skipped_large += 1
                    continue
                if ext in CODE_EXTENSIONS:
                    try:
                        content = item.decoded_content.decode("utf-8", errors="ignore")
                        if len(content) > max_file_chars:
                            content = content[:max_file_chars] + "\n# ... truncated"
                        entry = (item.path, content)
                        if item.name in PRIORITY_FILES:
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

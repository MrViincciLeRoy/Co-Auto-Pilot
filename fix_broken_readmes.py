import sys
import logging
import argparse
from github import Github, GithubException

from src.config import load_config
from src.groq_client import AIClient, AllKeysDead
from src.github_utils import collect_files
from src.analyzer import analyze_repo

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


def _is_broken_readme(content: str) -> bool:
    """Detect literal \n sequences that should be real newlines."""
    if not content:
        return False
    literal_newlines = content.count("\\n")
    real_newlines = content.count("\n")
    # Broken if it has many literal \n and almost no real newlines
    return literal_newlines > 5 and real_newlines < 3


def _get_readme(repo):
    for name in ("README.md", "readme.md", "Readme.md"):
        try:
            f = repo.get_contents(name)
            content = f.decoded_content.decode("utf-8", errors="ignore")
            return content, f.sha, name
        except GithubException:
            pass
    return None, None, None


def fix_repo(repo, ai: AIClient, cfg: dict, dry_run: bool) -> bool:
    content, sha, filename = _get_readme(repo)

    if content is None:
        log.info(f"  SKIP  {repo.name:<35}  no README")
        return False

    if not _is_broken_readme(content):
        log.info(f"  SKIP  {repo.name:<35}  README looks fine")
        return False

    log.info(f"  FIX   {repo.name}  — broken README detected")

    s = cfg.get("scanner", {})
    files, total = collect_files(repo, s)
    count = len(files["priority"]) + len(files["normal"])

    if count == 0:
        log.info("        no code files — skipping")
        return False

    log.info(f"        {count} file(s) · {total:,} chars")

    try:
        result = analyze_repo(ai, repo.name, files)
    except AllKeysDead:
        raise
    except Exception as e:
        log.error(f"        error: {e}")
        return False

    if not result:
        return False

    readme = result.get("readme", "").strip()
    if not readme:
        log.warning("        AI returned empty readme — skipping")
        return False

    if dry_run:
        log.info(f"        [DRY RUN] would rewrite {filename}")
        log.info(f"        preview: {readme[:120]}...")
        return True

    try:
        repo.update_file(filename, "fix: rewrite malformed README via repo-scanner", readme, sha)
        log.info(f"        ✓ {filename} rewritten")
        return True
    except Exception as e:
        log.error(f"        push failed: {e}")
        return False


def run_fix(cfg: dict, dry_run: bool, skip_repo: str = None):
    log.info("=" * 55)
    log.info("Broken README fixer — starting")

    token = cfg.get("github", {}).get("token", "")
    if not token:
        raise SystemExit("No GitHub token")

    from github import Auth
    gh = Github(auth=Auth.Token(token))
    ai = AIClient(cfg)

    gh_cfg = cfg.get("github", {})
    username = gh_cfg.get("username", "").strip()
    user = gh.get_user(username) if (username and username != "your_github_username") else gh.get_user()

    # Auto-detect current repo name if not provided
    current_repo = skip_repo or gh.get_repo(f"{user.login}/{user.login}").name if False else skip_repo

    repos = list(user.get_repos(type="owner"))
    skip_forks = cfg.get("scanner", {}).get("skip_forks", True)

    log.info(f"Repos: {len(repos)}  skip_self={current_repo or 'none'}  dry_run={dry_run}")
    log.info("-" * 55)

    fixed = 0
    for repo in repos:
        if skip_forks and repo.fork:
            log.info(f"  SKIP  {repo.name:<35}  forked")
            continue
        if current_repo and repo.name == current_repo:
            log.info(f"  SKIP  {repo.name:<35}  this repo (self)")
            continue
        try:
            if fix_repo(repo, ai, cfg, dry_run):
                fixed += 1
        except AllKeysDead as e:
            log.error("=" * 55)
            log.error(f"FATAL: {e}")
            log.error("=" * 55)
            sys.exit(1)
        except Exception as e:
            log.error(f"  ERROR  {repo.name}: {e}")

    log.info("-" * 55)
    log.info(f"Done — {fixed} README(s) {'would be ' if dry_run else ''}fixed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix repos with malformed READMEs (literal \\n)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Log only, push nothing")
    parser.add_argument("--skip-repo", default=None, help="Repo name to skip (the repo running this script)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    run_fix(cfg, dry_run=args.dry_run, skip_repo=args.skip_repo)

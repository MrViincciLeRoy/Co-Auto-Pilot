import sys
import logging
import argparse
from github import Github, GithubException

from src.config import load_config
from src.groq_client import AIClient, AllKeysDead
from src.github_utils import get_readme_state, collect_files
from src.analyzer import analyze_repo, PayloadTooLargeError

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


def force_process_repo(repo, ai: AIClient, cfg: dict, dry_run: bool) -> bool:
    s = cfg.get("scanner", {})
    overwrite = s.get("overwrite_readme", False)

    readme_state, readme_sha = get_readme_state(repo)
    needs_desc = not repo.description or readme_state == "stub"
    needs_readme = overwrite or readme_state in ("missing", "empty", "stub")

    if not needs_desc and not needs_readme:
        log.info(f"  SKIP  {repo.name:<35}  already documented")
        return False

    reasons = []
    if not repo.description:
        reasons.append("no description")
    if readme_state in ("missing", "empty", "stub"):
        reasons.append(f"{readme_state} README")

    log.info(f"  SCAN  {repo.name}  ({', '.join(reasons)})")

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
    except PayloadTooLargeError:
        log.error("        payload too large — skipping")
        return False
    except Exception as e:
        log.error(f"        error: {e}")
        return False

    if not result:
        return False

    desc = result.get("description", "")[:255]
    readme = result.get("readme", "").strip()

    if not desc and not readme:
        log.warning("        AI returned empty result — skipping")
        return False

    if dry_run:
        log.info(f"        [DRY RUN] desc   → {desc}")
        log.info(f"        [DRY RUN] readme → {readme[:120]}...")
        return True

    updated = False

    if needs_desc and desc:
        try:
            repo.edit(description=desc)
            log.info("        ✓ description set")
            updated = True
        except Exception as e:
            log.error(f"        description failed: {e}")

    if needs_readme and readme:
        try:
            if readme_state in ("empty", "stub") and readme_sha:
                msg = "docs: fill empty README" if readme_state == "empty" else "docs: rewrite stub README"
                repo.update_file("README.md", f"{msg} via repo-scanner", readme, readme_sha)
                log.info(f"        ✓ README {'filled' if readme_state == 'empty' else 'rewritten'}")
                updated = True
            elif readme_state == "missing":
                repo.create_file("README.md", "docs: add README via repo-scanner", readme)
                log.info("        ✓ README created")
                updated = True
        except Exception as e:
            log.error(f"        README failed: {e}")

    return updated


def run_force_scan(cfg: dict, dry_run: bool, skip_repo: str = None):
    log.info("=" * 55)
    log.info("Force scan all repos — ignoring 30-day activity limit")

    token = cfg.get("github", {}).get("token", "")
    if not token:
        raise SystemExit("No GitHub token")

    from github import Auth
    gh = Github(auth=Auth.Token(token))
    ai = AIClient(cfg)

    gh_cfg = cfg.get("github", {})
    username = gh_cfg.get("username", "").strip()
    user = gh.get_user(username) if (username and username != "your_github_username") else gh.get_user()

    repos = list(user.get_repos(type="owner"))
    skip_forks = cfg.get("scanner", {}).get("skip_forks", True)

    log.info(f"Repos: {len(repos)}  skip_self={skip_repo or 'none'}  dry_run={dry_run}")
    log.info("-" * 55)

    updated = 0
    for repo in repos:
        if skip_forks and repo.fork:
            log.info(f"  SKIP  {repo.name:<35}  forked")
            continue
        if skip_repo and repo.name == skip_repo:
            log.info(f"  SKIP  {repo.name:<35}  this repo (self)")
            continue
        try:
            if force_process_repo(repo, ai, cfg, dry_run):
                updated += 1
        except AllKeysDead as e:
            log.error("=" * 55)
            log.error(f"FATAL: {e}")
            log.error("=" * 55)
            sys.exit(1)
        except Exception as e:
            log.error(f"  ERROR  {repo.name}: {e}")

    log.info("-" * 55)
    log.info(f"Done — {updated} repo(s) {'would be ' if dry_run else ''}updated")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Force scan all repos regardless of activity")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Log only, push nothing")
    parser.add_argument("--skip-repo", default=None, help="Repo name to skip (this repo)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    run_force_scan(cfg, dry_run=args.dry_run, skip_repo=args.skip_repo)

"""
delete.py — Remove an AP3 platform instance from all tooling.

Reverses what bootstrap did:
  1. Delete all repos in the GitHub/Gitea org (platform, shared-lib, libraries,
     and any service repos created with 'svc create')
  2. Delete Jenkins jobs for all registered services
  3. Remove Jenkins global shared library configuration
  4. Delete SonarQube projects for all registered services
  5. Remove the local platform instance directory
  6. Reset platform/envs/, platform/services/, and platform/platform.yaml in
     the toolkit to their template state and commit 'chore: platform reset'

Usage (via bootstrap/delete.sh):
    python bootstrap/scripts/delete.py [options]

Flags:
    --keep-repos              Skip GitHub/Gitea repo deletion
    --keep-jenkins            Skip Jenkins job deletion
    --keep-jenkins-lib        Skip Jenkins global library removal
    --keep-sonar              Skip SonarQube project deletion
    --keep-local              Skip local platform-instance directory removal
    --keep-platform-state     Skip resetting the toolkit platform/ template state
    --yes                     Skip confirmation prompts
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml
import requests

# Make bootstrap/scripts/ importable (shared copies of config/output/identity)
sys.path.insert(0, os.path.dirname(__file__))
from output import success, step, warn, out


# ── Helpers ────────────────────────────────────────────────────────────────────

def _git_api_headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }


def _resolve_api_base(github_url: str, api_path: str) -> str:
    github_url = github_url.rstrip("/")
    if "github.com" in github_url:
        return "https://api.github.com"
    if api_path:
        return f"{github_url}/{api_path.lstrip('/')}"
    # No api_path in state (written by older bootstrap) — probe Gitea vs bare GitHub Enterprise.
    # Gitea uses /api/v1; GitHub Enterprise uses /api/v3.
    try:
        import requests as _req
        r = _req.get(f"{github_url}/api/v1/version", timeout=5)
        if r.status_code == 200:
            return f"{github_url}/api/v1"
    except Exception:
        pass
    return f"{github_url}/api/v3"


# ── Confirmation ───────────────────────────────────────────────────────────────

def _confirm(state: dict, yes: bool):
    if yes:
        return
    platform_dir = state.get("platform_target_dir", "(unknown)")
    github_url   = state.get("github_url", "")
    github_org   = state.get("github_org", "")
    jenkins_url  = state.get("jenkins_url", "")

    print()
    print("  !! WARNING — this operation is DESTRUCTIVE and IRREVERSIBLE !!")
    print()
    print(f"  Platform directory : {platform_dir}")
    print(f"  GitHub org         : {github_org}  ({github_url})")
    print(f"  Jenkins            : {jenkins_url}")
    print()
    print("  The following will be permanently deleted:")
    print("    • All repos in the GitHub/Gitea org (platform, shared-lib,")
    print("      libraries, and any service repos)")
    print("    • Jenkins pipeline jobs for all registered services")
    print("    • Jenkins global shared library configuration")
    print("    • SonarQube projects for all registered services")
    print("    • Local platform instance directory")
    print("    • Toolkit platform/ state (envs/, services/, platform.yaml reset)")
    print()
    answer = input("  Type  DELETE  to confirm: ").strip()
    if answer != "DELETE":
        print("\n  Aborted.")
        sys.exit(0)
    print()


# ── GitHub / Gitea ─────────────────────────────────────────────────────────────

def _delete_repo(api_base: str, org: str, repo: str, token: str) -> bool:
    """Delete a single repo. Returns True on success or already-gone."""
    resp = requests.delete(
        f"{api_base}/repos/{org}/{repo}",
        headers=_git_api_headers(token),
        timeout=15,
    )
    if resp.status_code == 204:
        success(f"Deleted repo {org}/{repo}")
        return True
    if resp.status_code == 404:
        warn(f"Repo {org}/{repo} not found — already deleted?")
        return True
    warn(f"Failed to delete {org}/{repo}: HTTP {resp.status_code} — {resp.text[:100]}")
    return False


def delete_repos(state: dict, token: str, skip: bool):
    if skip:
        out("  --keep-repos: skipping repo deletion.")
        return

    github_url = state["github_url"].rstrip("/")
    github_org = state["github_org"]
    api_path   = state.get("github_api_path", "")
    api_base   = _resolve_api_base(github_url, api_path)

    step(f"Deleting all repositories in org '{github_org}'")

    # List ALL repos in the org (paginated) and delete them.
    # This catches platform, shared-lib, libraries, AND any service repos
    # created with 'svc create' during testing — not just state-listed ones.
    #
    # GitHub uses per_page; Gitea uses limit.  Pass both — each ignores the other.
    account_type = state.get("github_account_type", "org")
    if account_type == "user":
        list_url = f"{api_base}/users/{github_org}/repos"
    else:
        list_url = f"{api_base}/orgs/{github_org}/repos"

    page = 1
    page_size = 100
    total = 0
    while True:
        resp = requests.get(
            list_url,
            headers=_git_api_headers(token),
            params={"per_page": page_size, "limit": page_size, "page": page},
            timeout=15,
        )
        if resp.status_code != 200:
            warn(f"Failed to list org repos: HTTP {resp.status_code} — {resp.text[:100]}")
            break
        repos = resp.json()
        if not repos:
            break
        for repo in repos:
            _delete_repo(api_base, github_org, repo["name"], token)
            total += 1
        if len(repos) < page_size:
            break
        page += 1

    if total == 0:
        warn(f"No repos found in org '{github_org}' — already deleted?")
    else:
        success(f"Org '{github_org}' cleared ({total} repo(s) deleted)")


# ── Jenkins ────────────────────────────────────────────────────────────────────

def _jenkins_crumb(jenkins_url: str, auth: tuple) -> dict:
    resp = requests.get(f"{jenkins_url}/crumbIssuer/api/json", auth=auth, timeout=10)
    if resp.status_code == 404:
        return {}   # CSRF protection disabled — no crumb needed
    if resp.status_code == 200:
        c = resp.json()
        return {c["crumbRequestField"]: c["crumb"]}
    warn(f"Could not fetch Jenkins crumb: HTTP {resp.status_code}")
    return {}


def _services_from_dir(services_dir: Path) -> list[str]:
    """
    Return service names found in a services/ directory.
    Services are stored as subdirectories: services/{name}/service.yaml
    """
    names = []
    if not services_dir or not services_dir.is_dir():
        return names
    for p in sorted(services_dir.iterdir()):
        if p.is_dir() and (p / "service.yaml").exists():
            names.append(p.name)
    return names


def delete_jenkins_jobs(state: dict, auth: tuple, skip: bool):
    if skip:
        out("  --keep-jenkins: skipping Jenkins job deletion.")
        return

    platform_dir = state.get("platform_target_dir", "")
    services_dir = Path(platform_dir) / "services" if platform_dir else None
    services = _services_from_dir(services_dir)

    if not services:
        out("  No registered services found — skipping Jenkins job deletion.")
        return

    step("Deleting Jenkins pipeline jobs")
    jenkins_url = state["jenkins_url"].rstrip("/")
    crumb = _jenkins_crumb(jenkins_url, auth)

    for service_name in services:
        resp = requests.post(
            f"{jenkins_url}/job/{service_name}/doDelete",
            auth=auth,
            headers=crumb,
            timeout=15,
        )
        if resp.status_code in (200, 302):
            success(f"Deleted Jenkins job: {service_name}")
        elif resp.status_code == 404:
            warn(f"Jenkins job '{service_name}' not found — already deleted?")
        else:
            warn(f"Failed to delete Jenkins job '{service_name}': HTTP {resp.status_code}")


def remove_jenkins_shared_lib(state: dict, auth: tuple, skip: bool):
    if skip:
        out("  --keep-jenkins-lib: skipping Jenkins shared library removal.")
        return

    step("Removing Jenkins global shared library 'platform-shared-lib'")
    jenkins_url = state["jenkins_url"].rstrip("/")
    crumb = _jenkins_crumb(jenkins_url, auth)

    groovy = """
import jenkins.model.Jenkins
import org.jenkinsci.plugins.workflow.libs.*

def globalLibraries = Jenkins.get().getDescriptor(GlobalLibraries.class)
globalLibraries.libraries = globalLibraries.libraries.findAll { it.name != 'platform-shared-lib' }
Jenkins.get().save()
println "DONE: platform-shared-lib removed"
""".strip()

    resp = requests.post(
        f"{jenkins_url}/script",
        auth=auth,
        headers={"Content-Type": "application/x-www-form-urlencoded", **crumb},
        data={"script": groovy},
        timeout=30,
    )
    if resp.status_code == 200 and "DONE:" in resp.text:
        success("Jenkins global shared library removed")
    else:
        warn(f"Jenkins script console: HTTP {resp.status_code} — {resp.text[:200]}")


# ── SonarQube ──────────────────────────────────────────────────────────────────

def delete_sonar_projects(state: dict, sonar_token: str, skip: bool):
    if skip:
        out("  --keep-sonar: skipping SonarQube project deletion.")
        return

    sonarqube_url = state.get("sonarqube_url", "").rstrip("/")
    if not sonarqube_url:
        warn("sonarqube_url not set in bootstrap state — skipping SonarQube cleanup.")
        return

    platform_dir = state.get("platform_target_dir", "")
    services_dir = Path(platform_dir) / "services" if platform_dir else None
    services = _services_from_dir(services_dir)

    if not services:
        out("  No registered services found — skipping SonarQube project deletion.")
        return

    step("Deleting SonarQube projects")
    auth = (sonar_token, "")

    for service_name in services:
        resp = requests.post(
            f"{sonarqube_url}/api/projects/delete",
            auth=auth,
            params={"project": service_name},
            timeout=15,
        )
        if resp.status_code in (200, 204):
            success(f"Deleted SonarQube project: {service_name}")
        elif resp.status_code == 404:
            warn(f"SonarQube project '{service_name}' not found — already deleted?")
        else:
            warn(f"Failed to delete SonarQube project '{service_name}': HTTP {resp.status_code}")


# ── Local platform instance ────────────────────────────────────────────────────

def remove_local_directory(state: dict, skip: bool):
    if skip:
        out("  --keep-local: skipping local platform-instance directory removal.")
        return

    platform_dir = state.get("platform_target_dir", "")
    if not platform_dir or not Path(platform_dir).exists():
        warn(f"Platform directory not found at '{platform_dir}' — already removed?")
        return

    step(f"Removing local platform instance: {platform_dir}")
    shutil.rmtree(platform_dir)
    success(f"Removed {platform_dir}")


# ── Toolkit platform/ state reset ─────────────────────────────────────────────

def reset_platform_state(skip: bool):
    """
    Clean platform/envs/ and platform/services/ in the toolkit repo and commit
    with 'chore: platform reset'.  This marker is used by history.py as the
    start-of-history boundary for the audit log.
    """
    if skip:
        out("  --keep-platform-state: skipping toolkit platform state reset.")
        return

    # Locate the toolkit root: bootstrap/scripts/delete.py → bootstrap/ → toolkit/
    bootstrap_dir = Path(__file__).parent.parent
    toolkit_root  = bootstrap_dir.parent
    platform_src  = toolkit_root / "platform"

    if not platform_src.is_dir():
        warn(f"Toolkit platform/ not found at {platform_src} — skipping state reset.")
        return

    step(f"Resetting toolkit platform state ({platform_src})")

    # ── Clean envs/ — remove everything except .gitkeep ──────────────────────
    envs_dir = platform_src / "envs"
    if envs_dir.is_dir():
        for p in envs_dir.iterdir():
            if p.name == ".gitkeep":
                continue
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        out("  platform/envs/ cleared")

    # ── Clean services/ — remove everything except .gitkeep ──────────────────
    services_dir = platform_src / "services"
    if services_dir.is_dir():
        for p in services_dir.iterdir():
            if p.name == ".gitkeep":
                continue
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        out("  platform/services/ cleared")

    # ── Reset platform.yaml via git checkout ──────────────────────────────────
    result = subprocess.run(
        ["git", "checkout", "--", "platform/platform.yaml"],
        cwd=str(toolkit_root),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        out("  platform/platform.yaml reset via git")
    else:
        warn(f"  Could not reset platform.yaml: {result.stderr.strip()}")

    # ── Commit the clean state ────────────────────────────────────────────────
    # history.py treats 'chore: platform reset' as the start-of-history marker.
    subprocess.run(
        ["git", "add",
         "platform/envs/",
         "platform/services/",
         "platform/platform.yaml"],
        cwd=str(toolkit_root),
        capture_output=True,
    )
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(toolkit_root),
        capture_output=True,
    )
    if diff.returncode == 0:
        out("  Nothing changed in toolkit platform state — no reset commit needed.")
    else:
        result = subprocess.run(
            ["git", "commit", "-m", "chore: platform reset"],
            cwd=str(toolkit_root),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            success("Reset commit created (audit log start-of-history marker)")
        else:
            warn(f"  Could not create reset commit: {result.stderr.strip()}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Remove an AP3 platform instance from all tooling."
    )
    parser.add_argument("--state", metavar="FILE",
                        help="Bootstrap state file (default: bootstrap/.bootstrap-state.yaml)")
    parser.add_argument("--config", "-c", metavar="FILE",
                        help="Bootstrap config file (alternative to --state)")
    parser.add_argument("--keep-repos",           action="store_true",
                        help="Skip GitHub/Gitea repo deletion")
    parser.add_argument("--keep-jenkins",         action="store_true",
                        help="Skip Jenkins job deletion")
    parser.add_argument("--keep-jenkins-lib",     action="store_true",
                        help="Skip Jenkins global library removal")
    parser.add_argument("--keep-sonar",           action="store_true",
                        help="Skip SonarQube project deletion")
    parser.add_argument("--keep-local",           action="store_true",
                        help="Skip local platform-instance directory removal")
    parser.add_argument("--keep-platform-state",  action="store_true",
                        help="Skip resetting toolkit platform/ state and git commit")
    parser.add_argument("--yes", "-y",            action="store_true",
                        help="Skip confirmation prompt")
    args = parser.parse_args()

    # ── Load state ────────────────────────────────────────────────────────────
    bootstrap_dir = Path(__file__).parent.parent
    default_state = bootstrap_dir / ".bootstrap-state.yaml"

    state_file  = Path(args.state)  if args.state  else default_state
    config_file = Path(args.config) if args.config else None

    state: dict = {}
    if state_file.exists():
        with open(state_file) as f:
            state = yaml.safe_load(f) or {}
    elif config_file and config_file.exists():
        with open(config_file) as f:
            state = yaml.safe_load(f) or {}
    else:
        print(f"[error] No state file found at {state_file}.")
        print("        Run with --state <file> or --config <file>.")
        sys.exit(1)

    # Resolve platform_target_dir to an absolute path (it may be relative in
    # bootstrap-config.yaml, e.g. "../platform-instance").
    if "platform_target_dir" in state:
        raw = state["platform_target_dir"]
        state["platform_target_dir"] = str(Path(raw).resolve())

    # ── Credentials ───────────────────────────────────────────────────────────
    github_token  = os.environ.get("GITHUB_TOKEN", "")
    jenkins_user  = os.environ.get("JENKINS_USER", "")
    jenkins_token = os.environ.get("JENKINS_TOKEN", "")
    sonar_token   = os.environ.get("SONARQUBE_TOKEN", "")

    missing = []
    if not github_token and not args.keep_repos:
        missing.append("  GITHUB_TOKEN  — needed to delete Gitea repos (or pass --keep-repos)")
    if (not jenkins_user or not jenkins_token) and not args.keep_jenkins and not args.keep_jenkins_lib:
        missing.append("  JENKINS_USER / JENKINS_TOKEN  — needed for Jenkins cleanup (or pass --keep-jenkins --keep-jenkins-lib)")
    if missing:
        print("\n  [error] Required environment variables are not set:\n")
        for m in missing:
            print(m)
        print("\n  Export them first, e.g.:")
        print("    set -a && source testenv/.env && set +a")
        sys.exit(1)

    jenkins_auth = (jenkins_user, jenkins_token)

    # ── Run ───────────────────────────────────────────────────────────────────
    _confirm(state, args.yes)

    delete_repos(state, github_token, skip=args.keep_repos)
    delete_jenkins_jobs(state, jenkins_auth, skip=args.keep_jenkins)
    remove_jenkins_shared_lib(state, jenkins_auth, skip=args.keep_jenkins_lib)
    delete_sonar_projects(state, sonar_token, skip=args.keep_sonar)
    remove_local_directory(state, skip=args.keep_local)
    reset_platform_state(skip=args.keep_platform_state)

    # Remove bootstrap state file on success so bootstrap can start fresh.
    if state_file.exists():
        state_file.unlink()
        success(f"Removed bootstrap state: {state_file}")

    print()
    success("Platform deleted. Re-run bootstrap to start fresh.")
    print()


if __name__ == "__main__":
    main()

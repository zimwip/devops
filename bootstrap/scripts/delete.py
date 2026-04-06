"""
delete.py — Remove an AP3 platform instance from all tooling.

Reverses what bootstrap did:
  1. Delete GitHub/Gitea repos (platform + shared-lib + extra libraries)
  2. Delete Jenkins jobs for all registered services
  3. Remove Jenkins global shared library configuration
  4. Delete SonarQube projects for all registered services
  5. Optionally remove the local platform instance directory

Usage (via bootstrap/delete.sh):
    python bootstrap/scripts/delete.py --state bootstrap/.bootstrap-state.yaml [options]

Flags:
    --keep-repos         Skip GitHub/Gitea repo deletion
    --keep-jenkins       Skip Jenkins job deletion
    --keep-jenkins-lib   Skip Jenkins global library removal
    --keep-sonar         Skip SonarQube project deletion
    --keep-local         Skip local directory removal
    --yes                Skip confirmation prompts
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml
import requests

# Make bootstrap/scripts/ importable (shared copies of config/output/identity)
sys.path.insert(0, os.path.dirname(__file__))
from output import success, step, warn, out, err


# ── Confirmation ───────────────────────────────────────────────────────────────

def _confirm(state: dict, yes: bool):
    if yes:
        return
    platform_dir = state.get("platform_target_dir", "(unknown)")
    github_url   = state.get("github_url", "")
    github_org   = state.get("github_org", "")
    repo_name    = state.get("platform_repo_name", "platform")
    jenkins_url  = state.get("jenkins_url", "")

    print()
    print("  !! WARNING — this operation is DESTRUCTIVE and IRREVERSIBLE !!")
    print()
    print(f"  Platform directory : {platform_dir}")
    print(f"  GitHub org/repo    : {github_org}/{repo_name}  ({github_url})")
    print(f"  Jenkins            : {jenkins_url}")
    print()
    print("  The following will be permanently deleted:")
    print("    • GitHub repos: platform, jenkins-shared-lib, extra libraries")
    print("    • Jenkins pipeline jobs for all registered services")
    print("    • Jenkins global shared library configuration")
    print("    • SonarQube projects for all registered services")
    print("    • Local platform directory")
    print()
    answer = input("  Type  DELETE  to confirm: ").strip()
    if answer != "DELETE":
        print("\n  Aborted.")
        sys.exit(0)
    print()


# ── GitHub / Gitea ─────────────────────────────────────────────────────────────

def _github_delete_repo(api_base: str, org: str, repo: str, token: str):
    """Delete a single repo. Returns True on success, False on skip."""
    url = f"{api_base}/repos/{org}/{repo}"
    resp = requests.delete(url,
                           headers={"Authorization": f"token {token}",
                                    "Accept": "application/vnd.github.v3+json"},
                           timeout=15)
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

    step("Deleting GitHub/Gitea repositories")

    github_url  = state["github_url"].rstrip("/")
    github_org  = state["github_org"]
    api_path    = state.get("github_api_path", "")

    if "github.com" in github_url:
        api_base = "https://api.github.com"
    elif api_path:
        api_base = f"{github_url}/{api_path.lstrip('/')}"
    else:
        api_base = f"{github_url}/api/v3"

    repos = [
        state.get("platform_repo_name", "platform"),
        state.get("shared_lib_repo_name", "jenkins-shared-lib"),
    ]
    # Extra libraries from state
    for lib in state.get("libraries", {}).keys():
        repos.append(lib)

    for repo in repos:
        _github_delete_repo(api_base, github_org, repo, token)


# ── Jenkins ────────────────────────────────────────────────────────────────────

def _jenkins_crumb(jenkins_url: str, auth: tuple) -> dict:
    resp = requests.get(f"{jenkins_url}/crumbIssuer/api/json", auth=auth, timeout=10)
    if resp.status_code == 404:
        return {}
    if resp.status_code == 200:
        c = resp.json()
        return {c["crumbRequestField"]: c["crumb"]}
    warn(f"Could not fetch Jenkins crumb: HTTP {resp.status_code}")
    return {}


def delete_jenkins_jobs(state: dict, auth: tuple, skip: bool):
    if skip:
        out("  --keep-jenkins: skipping Jenkins job deletion.")
        return

    platform_dir = state.get("platform_target_dir", "")
    services_dir = Path(platform_dir) / "services" if platform_dir else None
    if not services_dir or not services_dir.is_dir():
        warn(f"Services directory not found at {services_dir} — skipping Jenkins cleanup.")
        return

    step("Deleting Jenkins pipeline jobs")
    jenkins_url = state["jenkins_url"].rstrip("/")
    crumb = _jenkins_crumb(jenkins_url, auth)

    for svc_path in sorted(services_dir.iterdir()):
        if not svc_path.is_dir():
            continue
        service_name = svc_path.name
        resp = requests.post(
            f"{jenkins_url}/job/{service_name}/doDelete",
            auth=auth,
            headers=crumb,
            timeout=15,
        )
        if resp.status_code in (200, 302):
            success(f"Deleted Jenkins job: {service_name}")
        elif resp.status_code == 404:
            warn(f"Jenkins job {service_name} not found — already deleted?")
        else:
            warn(f"Failed to delete Jenkins job {service_name}: HTTP {resp.status_code}")


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
    if not services_dir or not services_dir.is_dir():
        warn(f"Services directory not found at {services_dir} — skipping SonarQube cleanup.")
        return

    step("Deleting SonarQube projects")
    auth = (sonar_token, "")

    for svc_path in sorted(services_dir.iterdir()):
        if not svc_path.is_dir():
            continue
        project_key = svc_path.name
        resp = requests.post(
            f"{sonarqube_url}/api/projects/delete",
            auth=auth,
            params={"project": project_key},
            timeout=15,
        )
        if resp.status_code in (200, 204):
            success(f"Deleted SonarQube project: {project_key}")
        elif resp.status_code == 404:
            warn(f"SonarQube project {project_key} not found — already deleted?")
        else:
            warn(f"Failed to delete SonarQube project {project_key}: HTTP {resp.status_code}")


# ── Local directory ────────────────────────────────────────────────────────────

def remove_local_directory(state: dict, skip: bool):
    if skip:
        out("  --keep-local: skipping local directory removal.")
        return

    import shutil
    platform_dir = state.get("platform_target_dir", "")
    if not platform_dir or not Path(platform_dir).exists():
        warn(f"Platform directory not found at {platform_dir} — already removed?")
        return

    step(f"Removing local platform directory: {platform_dir}")
    shutil.rmtree(platform_dir)
    success(f"Removed {platform_dir}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Remove an AP3 platform instance from all tooling."
    )
    parser.add_argument("--state", metavar="FILE",
                        help="Bootstrap state file (default: bootstrap/.bootstrap-state.yaml)")
    parser.add_argument("--config", "-c", metavar="FILE",
                        help="Bootstrap config file (alternative to --state)")
    parser.add_argument("--keep-repos",       action="store_true")
    parser.add_argument("--keep-jenkins",     action="store_true")
    parser.add_argument("--keep-jenkins-lib", action="store_true")
    parser.add_argument("--keep-sonar",       action="store_true")
    parser.add_argument("--keep-local",       action="store_true")
    parser.add_argument("--yes", "-y",        action="store_true",
                        help="Skip confirmation prompts")
    args = parser.parse_args()

    # ── Load state ────────────────────────────────────────────────────────────
    bootstrap_dir = Path(__file__).parent.parent
    default_state = bootstrap_dir / ".bootstrap-state.yaml"

    state_file = Path(args.state) if args.state else default_state
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

    # Override platform_target_dir if given in config
    if config_file and config_file.exists() and "platform_target_dir" in state:
        raw = state["platform_target_dir"]
        state["platform_target_dir"] = str(Path(raw).resolve())

    # ── Credentials ───────────────────────────────────────────────────────────
    github_token  = os.environ.get("GITHUB_TOKEN", "")
    jenkins_user  = os.environ.get("JENKINS_USER", "")
    jenkins_token = os.environ.get("JENKINS_TOKEN", "")
    sonar_token   = os.environ.get("SONARQUBE_TOKEN", "")

    if not github_token and not args.keep_repos:
        warn("GITHUB_TOKEN not set — repo deletion will fail. Use --keep-repos to skip.")
    if not jenkins_user or not jenkins_token:
        if not args.keep_jenkins and not args.keep_jenkins_lib:
            warn("JENKINS_USER / JENKINS_TOKEN not set — Jenkins cleanup will fail.")

    jenkins_auth = (jenkins_user, jenkins_token)

    # ── Run ───────────────────────────────────────────────────────────────────
    _confirm(state, args.yes)

    delete_repos(state, github_token, skip=args.keep_repos)
    delete_jenkins_jobs(state, jenkins_auth, skip=args.keep_jenkins)
    remove_jenkins_shared_lib(state, jenkins_auth, skip=args.keep_jenkins_lib)
    delete_sonar_projects(state, sonar_token, skip=args.keep_sonar)
    remove_local_directory(state, skip=args.keep_local)

    # Remove bootstrap state file on success
    if state_file.exists():
        state_file.unlink()
        success(f"Removed bootstrap state file: {state_file}")

    print()
    success("Platform deleted.")
    print()


if __name__ == "__main__":
    main()

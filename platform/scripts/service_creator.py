"""
service_creator.py — Bootstrap a new AP3 service.

Three source modes
──────────────────
  template   AP3 creates a new GitHub repo under the AP3 org, scaffolded
             from one of the built-in templates (springboot / react / python-api).
             This is a fully AP3-hosted service.

  fork       AP3 creates a new GitHub repo by forking an existing AP3-hosted
             service. The fork starts as a full copy of the source service,
             including its .ap3/hooks.yaml, Helm chart, and Jenkinsfile.
             The new service is independently versioned from the source.

  external   The service already exists in an external (or user-owned) GitHub
             repo. AP3 does NOT create or scaffold any repo. It only:
               • registers the service in the dev versions.yaml
               • creates a Jenkins multibranch pipeline pointing to the URL
               • optionally adds a minimal .ap3/hooks.yaml to the existing repo

  In all three cases a Jenkins pipeline is created and the service is
  registered in the dev environment versions.yaml.

AP3-hosted flag
───────────────
  ap3_hosted: true   → modes "template" and "fork"
  ap3_hosted: false  → mode "external"

Deployment hooks
─────────────────
  Any service (hosted or external) can include a .ap3/hooks.yaml in its
  repo root to customise build/deploy behaviour. See docs/service-hooks.md.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from config import PlatformConfig
from compat import run, IS_WINDOWS, set_executable
from output import out, step, success, warn, error_exit


_VALID_NAME = re.compile(r"^[a-z][a-z0-9-]{1,48}[a-z0-9]$")

# Default .ap3/hooks.yaml written into scaffolded services
_DEFAULT_HOOKS_YAML = """\
# .ap3/hooks.yaml — AP3 service hook configuration
# All fields are optional. See docs/service-hooks.md for full reference.

service:
  ap3_hosted: {ap3_hosted}

build:
  skip_quality_gate: false
  # extra_maven_args: ""
  # extra_npm_args: ""

deploy:
  rollback_on_failure: true
  health_check_path: /actuator/health
  health_check_timeout_s: 120
  # helm_extra_values:
  #   replicas: 2

# hooks:
#   pre_deploy:  .ap3/pre-deploy.sh
#   post_deploy: .ap3/post-deploy.sh
#   validate:    .ap3/validate.sh

# notifications:
#   slack_channel: "#my-service-deploys"
"""


class ServiceCreator:
    def __init__(self, cfg: PlatformConfig, dry_run=False, json_output=False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.json_output = json_output

    def _require_tokens(self, identity, needs_github: bool, needs_jenkins: bool):
        """Reject the action immediately if required tokens are absent or invalid.
        --dry-run bypasses all token checks."""
        if self.dry_run:
            return
        if needs_github:
            state = identity.github_token_state
            if state == "missing":
                error_exit(
                    f"{self.cfg.github_token_env} is not set. "
                    "All platform actions that touch Git hosting require a valid token. "
                    f"Set {self.cfg.github_token_env} and retry, or use --dry-run to simulate."
                )
            elif state == "invalid":
                error_exit(
                    f"{self.cfg.github_token_env} is invalid (401 Unauthorized). "
                    "Verify the token has the required scopes and retry, or use --dry-run to simulate."
                )
        if needs_jenkins:
            state = identity.jenkins_token_state
            if state == "missing":
                error_exit(
                    "JENKINS_TOKEN or JENKINS_USER is not set. "
                    "All platform actions that touch Jenkins require valid credentials. "
                    "Set JENKINS_USER + JENKINS_TOKEN and retry, pass --skip-jenkins to omit "
                    "pipeline registration, or use --dry-run to simulate."
                )
            elif state == "invalid":
                error_exit(
                    f"JENKINS_TOKEN is invalid (401 Unauthorized) for user "
                    f"'{self.cfg.jenkins_user_env}'. "
                    "Verify the credentials and retry, pass --skip-jenkins to omit "
                    "pipeline registration, or use --dry-run to simulate."
                )

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────────────────

    def create(
        self,
        name: str,
        owner: str,
        description: str = "",
        # Source mode
        source_mode: str = "template",    # "template" | "fork" | "external"
        template: str = "springboot",     # used when source_mode == "template"
        fork_from: str = "",              # used when source_mode == "fork"
        external_repo_url: str = "",      # used when source_mode == "external"
        # Flags
        skip_jenkins: bool = False,
        force: bool = False,
    ):
        self._validate_name(name)
        ap3_hosted = source_mode in ("template", "fork")

        # External repos are not AP3-managed — never auto-create a Jenkins job
        if source_mode == "external":
            skip_jenkins = True

        # ── Identity + token enforcement ──────────────────────────────────
        from identity import resolve_identity, format_disclaimer
        from output import confirm_with_actor
        identity = resolve_identity(self.cfg)
        self._require_tokens(identity,
                             needs_github=ap3_hosted,
                             needs_jenkins=not skip_jenkins)

        actions = self._build_actions(
            name, source_mode, template, fork_from,
            external_repo_url, skip_jenkins, identity, ap3_hosted,
        )
        if not self.dry_run and not self.json_output:
            confirm_with_actor(format_disclaimer(identity, actions), force=force)

        steps = []
        warnings = []

        def _warn(msg):
            warn(msg)
            warnings.append(msg)

        # ── Source mode dispatch ───────────────────────────────────────────
        if source_mode == "template":
            target, repo_url = self._mode_template(
                name, template, owner, description,
                ap3_hosted, steps, warnings,
            )
        elif source_mode == "fork":
            target, repo_url = self._mode_fork(
                name, fork_from, owner, description,
                steps, warnings,
            )
        elif source_mode == "external":
            target, repo_url = self._mode_external(
                name, external_repo_url, owner, description,
                steps, warnings,
            )
        else:
            error_exit(f"Unknown source_mode '{source_mode}'.")
            return  # unreachable

        # ── Jenkins pipeline ───────────────────────────────────────────────
        if not skip_jenkins:
            if not self.cfg.jenkins_token:
                _warn(
                    "JENKINS_TOKEN is not set — Jenkins pipeline registration "
                    "was skipped. Set JENKINS_USER + JENKINS_TOKEN and re-run, "
                    "or register the pipeline manually."
                )
                steps.append({"step": "jenkins_pipeline", "status": "skipped",
                               "reason": "JENKINS_TOKEN not set"})
            else:
                try:
                    self._register_jenkins_pipeline(name, repo_url)
                    steps.append({"step": "jenkins_pipeline", "status": "ok"})
                except Exception as e:
                    _warn(f"Jenkins pipeline registration failed: {e}")
                    steps.append({"step": "jenkins_pipeline",
                                  "status": "failed", "reason": str(e)})
        else:
            steps.append({"step": "jenkins_pipeline", "status": "skipped",
                          "reason": "--no-jenkins"})

        # ── Register in service catalog ───────────────────────────────────
        self._register_in_service_catalog(
            name, owner, description, source_mode, template, repo_url, ap3_hosted
        )
        steps.append({"step": "register_in_service_catalog", "status": "ok"})

        if not self.dry_run:
            self._git_commit(
                f"svc: register service '{name}'",
                stage_extra=["services/"],
            )

        # ── Output ────────────────────────────────────────────────────────
        result = {
            "service":     name,
            "source_mode": source_mode,
            "ap3_hosted":  ap3_hosted,
            "repo_url":    repo_url,
            "steps":       steps,
            "warnings":    warnings,
        }

        if self.json_output:
            print(json.dumps(result, indent=2))
        else:
            success(f"Service '{name}' bootstrapped ({source_mode} mode)!")
            print()
            for s in steps:
                icon = "+" if s["status"] == "ok" else ("~" if s["status"] == "skipped" else "x")
                detail = s.get("url", s.get("path", s.get("reason", "")))
                print(f"  {icon}  {s['step']}" + (f"  → {detail}" if detail else ""))
            print(f"\n  Repo : {repo_url}")
            if warnings:
                print()
                for w in warnings:
                    print(f"  !  {w}")
            print()
            print(f"  Deployments are pull-based: push to the 'develop' branch")
            print(f"  and Jenkins will deploy {name} to dev automatically.")

        return result

    def _validate_name(self, name: str):
        if not _VALID_NAME.match(name):
            error_exit(
                f"Invalid service name '{name}'. "
                "Use lowercase letters, digits and hyphens (3-50 chars, "
                "starting and ending with a letter or digit)."
            )

    # ─────────────────────────────────────────────────────────────────────────
    # SOURCE MODES
    # ─────────────────────────────────────────────────────────────────────────

    def _mode_template(
        self, name, template, owner, description, ap3_hosted, steps, warnings
    ) -> tuple[None, str]:
        """Scaffold into a temp dir → push to GitHub → discard temp dir."""
        with tempfile.TemporaryDirectory(prefix=f"ap3-scaffold-{name}-") as tmpdir:
            target = Path(tmpdir) / name
            self._scaffold(target, name, template, owner, description, ap3_hosted)
            steps.append({"step": "scaffold", "status": "ok"})

            self._git_init(target, name)
            steps.append({"step": "git_init", "status": "ok"})

            repo_url = self._github_create_and_push(name, description, owner,
                                                     target, steps, warnings)
        # temp dir is gone — scaffold never touches the platform repo
        return None, repo_url

    def _mode_fork(
        self, name, fork_from, owner, description, steps, warnings
    ) -> tuple[Path, str]:
        """Clone existing AP3 service → rename → new GitHub repo → push."""
        if not fork_from:
            error_exit("--fork-from is required in 'fork' mode.")

        step(f"Forking '{fork_from}' → '{name}'")
        source_url = self._repo_url(fork_from)

        with tempfile.TemporaryDirectory(prefix=f"ap3-fork-{name}-") as tmpdir:
            target = Path(tmpdir) / name

            if not self.dry_run:
                try:
                    run(["git", "clone", source_url, str(target)],
                        check=True, capture_output=True)
                except subprocess.CalledProcessError:
                    error_exit(
                        f"Could not clone '{fork_from}' from {source_url}. "
                        "Ensure the source service exists and GITHUB_TOKEN has read access."
                    )

                shutil.rmtree(target / ".git")
                self._replace_placeholders(target, name, owner, description)

                hooks_path = target / ".ap3" / "hooks.yaml"
                if hooks_path.exists():
                    hooks_data = yaml.safe_load(hooks_path.read_text()) or {}
                    hooks_data.setdefault("service", {})
                    hooks_data["service"]["fork_from"] = fork_from
                    hooks_data["service"]["ap3_hosted"] = True
                    hooks_path.write_text(
                        yaml.dump(hooks_data, default_flow_style=False, allow_unicode=True)
                    )
                else:
                    hooks_path.parent.mkdir(exist_ok=True)
                    hooks_path.write_text(
                        _DEFAULT_HOOKS_YAML.format(ap3_hosted="true")
                        + f"\n  # forked_from: {fork_from}\n"
                    )

                self._git_init(target, name)

            steps.append({"step": "fork_scaffold", "source": fork_from, "status": "ok"})
            repo_url = self._github_create_and_push(name, description, owner,
                                                     target, steps, warnings)
        # temp dir gone
        return None, repo_url

    def _mode_external(
        self, name, external_repo_url, owner, description, steps, warnings
    ) -> tuple[None, str]:
        """Register an externally-hosted service — no repo creation, no scaffold."""
        if not external_repo_url:
            error_exit("--external-repo-url is required in 'external' mode.")

        step(f"Registering external service '{name}' → {external_repo_url}")
        steps.append({
            "step":   "external_repo_registered",
            "url":    external_repo_url,
            "status": "ok",
        })

        # Optionally verify the repo is accessible
        if self.cfg.github_token and not self.dry_run:
            try:
                resp = requests.get(
                    external_repo_url.replace("https://github.com/", "https://api.github.com/repos/")
                    .rstrip(".git"),
                    headers={"Authorization": f"token {self.cfg.github_token}",
                              "Accept": "application/vnd.github.v3+json"},
                    timeout=5,
                )
                if resp.status_code == 200:
                    steps.append({"step": "repo_verified", "status": "ok"})
                else:
                    warnings.append(
                        f"Could not verify external repo (HTTP {resp.status_code}). "
                        "Check the URL and that GITHUB_TOKEN has read access."
                    )
            except Exception:
                pass  # network issue — not fatal

        return None, external_repo_url

    # ─────────────────────────────────────────────────────────────────────────
    # GITHUB HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _repo_url(self, service_name: str) -> str:
        """Return the HTTPS clone URL for an AP3-managed service."""
        base    = self.cfg.github_url.rstrip("/")
        account = self.cfg.github_account
        return f"{base}/{account}/{service_name}.git"

    def _github_create_and_push(
        self, name, description, owner, target, steps, warnings
    ) -> str:
        # Token was already checked in create() for ap3-hosted modes.
        # Any failure here is fatal — the operation is aborted and nothing is registered.
        repo_url = self._create_github_repo(name, description, owner)
        self._push_to_github(target, repo_url)
        self._set_default_branch(name, "develop")
        self._register_webhook(name)
        steps.append({"step": "github_repo", "url": repo_url, "status": "ok"})
        self._setup_branch_protection(name)
        steps.append({"step": "branch_protection", "status": "ok"})
        self._push_codeowners(name)
        steps.append({"step": "codeowners", "status": "ok"})
        return repo_url

    def _create_github_repo(self, name: str, description: str, owner: str) -> str:
        step(f"Creating GitHub repo {self.cfg.github_account}/{name} "
             f"[{self.cfg.github_account_type}]")
        if self.dry_run:
            return self._repo_url(name)
        resp = requests.post(
            self.cfg.github_repos_api(),
            headers={"Authorization": f"token {self.cfg.github_token}",
                     "Accept": "application/vnd.github.v3+json"},
            json={"name": name, "description": description, "private": True,
                  "auto_init": False, "has_issues": True,
                  "has_projects": False, "has_wiki": False},
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"GitHub API {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()["clone_url"]

    def _push_url_with_creds(self, repo_url: str) -> str:
        """Return repo_url with token embedded so git push never prompts for credentials."""
        import re as _re
        if not self.cfg.github_token or not repo_url.startswith("http"):
            return repo_url
        m = _re.match(r"(https?://)(.*)", repo_url)
        if not m:
            return repo_url
        # oauth2 as username works for Gitea, Gitea-over-HTTP, and github.com PATs
        return f"{m.group(1)}oauth2:{self.cfg.github_token}@{m.group(2)}"

    def _push_to_github(self, target: Path, repo_url: str):
        step(f"Pushing to {repo_url}")
        if self.dry_run:
            return
        push_url = self._push_url_with_creds(repo_url)
        for cmd in [
            ["git", "remote", "add", "origin", push_url],
            ["git", "push", "-u", "origin", "main"],
            ["git", "push", "-u", "origin", "develop"],
        ]:
            try:
                run(cmd, cwd=target, check=True, capture_output=True)
            except subprocess.CalledProcessError as e:
                stderr = e.stderr.decode(errors="replace").strip() if e.stderr else ""
                raise RuntimeError(
                    f"Git push to {repo_url} failed: {stderr or str(e)}"
                ) from e

    def _set_default_branch(self, name: str, branch: str = "develop"):
        """Set the default branch for the repo (shown in Gitea/GitHub UI and used for PRs)."""
        if self.dry_run or not self.cfg.github_token:
            return
        api = self.cfg.github_api_base
        account = self.cfg.github_account
        resp = requests.patch(
            f"{api}/repos/{account}/{name}",
            headers={"Authorization": f"token {self.cfg.github_token}",
                     "Accept": "application/vnd.github.v3+json"},
            json={"default_branch": branch},
        )
        if resp.status_code not in (200, 201):
            warn(f"Could not set default branch to '{branch}': HTTP {resp.status_code}")

    def _setup_branch_protection(self, name: str):
        step("Configuring branch protection rules")
        if self.dry_run or not self.cfg.github_token:
            return
        # Branch protection requires org admin rights — skip silently for user accounts
        if self.cfg.github_account_type == "user":
            warn("Branch protection skipped for user repos (requires org admin).")
            return
        api = self.cfg.github_api_base
        account = self.cfg.github_account
        for branch in ("main", "develop"):
            requests.put(
                f"{api}/repos/{account}/{name}/branches/{branch}/protection",
                headers={"Authorization": f"token {self.cfg.github_token}",
                         "Accept": "application/vnd.github.v3+json"},
                json={
                    "required_status_checks":
                        {"strict": True, "contexts": ["ci/jenkins"]},
                    "enforce_admins": True,
                    "required_pull_request_reviews": {
                        "required_approving_review_count": 1,
                        "dismiss_stale_reviews": True,
                    },
                    "restrictions": None,
                },
            )

    def _push_codeowners(self, name: str):
        """Push a .github/CODEOWNERS file so Jenkinsfile changes require DevOps approval."""
        step("Pushing CODEOWNERS file")
        if self.dry_run or not self.cfg.github_token:
            return
        if self.cfg.github_account_type == "user":
            return  # fine-grained team reviews require org context
        import base64
        api = self.cfg.github_api_base
        account = self.cfg.github_account
        content = "# Jenkinsfile changes require DevOps team approval\nJenkinsfile @devops-team\n"
        requests.put(
            f"{api}/repos/{account}/{name}/contents/.github/CODEOWNERS",
            headers={"Authorization": f"token {self.cfg.github_token}",
                     "Accept": "application/vnd.github.v3+json"},
            json={
                "message": "chore: add CODEOWNERS for Jenkinsfile protection",
                "content": base64.b64encode(content.encode()).decode(),
                "branch": "main",
            },
        )

    # ─────────────────────────────────────────────────────────────────────────
    # JENKINS
    # ─────────────────────────────────────────────────────────────────────────

    def _register_webhook(self, name: str):
        """Register a webhook on the service repo so pushes trigger Jenkins immediately.

        Works for both Gitea and GitHub:
        - Gitea: POST /api/v1/repos/{owner}/{repo}/hooks, type=gitea,
                 Jenkins receives at /gitea-webhook/post (gitea Jenkins plugin)
        - GitHub: POST /repos/{owner}/{repo}/hooks, type=web,
                  Jenkins receives at /github-webhook/ (GitHub Jenkins plugin)
        """
        if self.dry_run or not self.cfg.github_token:
            return
        hook_base = (self.cfg.jenkins_hook_url or self.cfg.jenkins_url).rstrip("/")
        if not hook_base:
            return

        api = self.cfg.github_api_base
        account = self.cfg.github_account
        is_github = "github.com" in self.cfg.github_url

        if is_github:
            webhook_url = f"{hook_base}/github-webhook/"
            payload = {
                "name": "web",
                "config": {"url": webhook_url, "content_type": "json"},
                "events": ["push", "pull_request"],
                "active": True,
            }
        else:
            # Gitea — Jenkins gitea plugin endpoint
            webhook_url = f"{hook_base}/gitea-webhook/post"
            payload = {
                "type": "gitea",
                "config": {"url": webhook_url, "content_type": "json"},
                "events": ["push", "pull_request"],
                "active": True,
            }

        resp = requests.post(
            f"{api}/repos/{account}/{name}/hooks",
            headers={"Authorization": f"token {self.cfg.github_token}",
                     "Accept": "application/vnd.github.v3+json"},
            json=payload,
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            warn(f"Webhook registration returned HTTP {resp.status_code} — "
                 f"pushes will trigger builds via periodic scan only")

    def _jenkins_crumb(self) -> dict:
        """Fetch a Jenkins CSRF crumb. Returns header dict (empty if crumb disabled)."""
        try:
            resp = requests.get(
                f"{self.cfg.jenkins_url}/crumbIssuer/api/json",
                auth=(self.cfg.jenkins_user, self.cfg.jenkins_token),
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                return {data["crumbRequestField"]: data["crumb"]}
        except Exception:
            pass
        return {}

    def _register_jenkins_pipeline(self, name: str, repo_url: str = ""):
        step(f"Registering Jenkins multibranch pipeline '{name}'")
        if self.dry_run:
            return
        config_xml = self._jenkins_pipeline_xml(name, repo_url)
        crumb = self._jenkins_crumb()
        resp = requests.post(
            f"{self.cfg.jenkins_url}/createItem?name={name}",
            auth=(self.cfg.jenkins_user, self.cfg.jenkins_token),
            headers={"Content-Type": "application/xml", **crumb},
            data=config_xml,
            timeout=15,
        )
        if resp.status_code == 400 and "already exists" in resp.text:
            # Job exists — update its config in place
            step(f"Jenkins job '{name}' exists — updating config")
            upd = requests.post(
                f"{self.cfg.jenkins_url}/job/{name}/config.xml",
                auth=(self.cfg.jenkins_user, self.cfg.jenkins_token),
                headers={"Content-Type": "application/xml", **crumb},
                data=config_xml,
                timeout=15,
            )
            if upd.status_code not in (200, 201):
                raise RuntimeError(
                    f"Jenkins config update returned HTTP {upd.status_code}: {upd.text[:300]}"
                )
            return
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Jenkins createItem returned HTTP {resp.status_code}: {resp.text[:300]}"
            )

    # GitFlow branches that Jenkins should discover and build.
    # feature/* and hotfix/* get build+test only (no deploy stage fires).
    # develop → dev deploy, release/* → staging deploy, main → prod gate.
    GITFLOW_BRANCH_REGEX = r"^(main|develop|release/.*|hotfix/.*|feature/.*|poc/.*)$"

    def _jenkins_pipeline_xml(self, name: str, repo_url: str = "") -> str:
        branch_filter = (
            f"          <jenkins.scm.impl.trait.RegexSCMHeadFilterTrait>\n"
            f"            <regex>{self.GITFLOW_BRANCH_REGEX}</regex>\n"
            f"          </jenkins.scm.impl.trait.RegexSCMHeadFilterTrait>\n"
        )
        # Periodic scan every 5 minutes — webhook is the primary trigger,
        # this is the fallback when the webhook fires before the job exists.
        scan_trigger = (
            f"  <triggers>\n"
            f"    <com.cloudbees.hudson.plugins.folder.computed.PeriodicFolderTrigger>\n"
            f"      <spec>H/5 * * * *</spec>\n"
            f"      <interval>300000</interval>\n"
            f"    </com.cloudbees.hudson.plugins.folder.computed.PeriodicFolderTrigger>\n"
            f"  </triggers>\n"
        )

        # Gitea — use GiteaSCMSource (gitea plugin): understands Gitea webhooks natively
        # and reports build status back to the PR.
        # serverUrl MUST match the URL registered in Jenkins via JCasC giteaServers.
        # That is jenkins_git_url (Docker-internal hostname), not the host-facing URL.
        if repo_url and repo_url.startswith("http") and "github.com" not in repo_url:
            server_url = (self.cfg.jenkins_git_url or self.cfg.github_url).rstrip("/")
            scm_block = (
                f"        <source class=\"org.jenkinsci.plugin.gitea.GiteaSCMSource\" plugin=\"gitea\">\n"
                f"          <id>{name}-scm</id>\n"
                f"          <serverUrl>{server_url}</serverUrl>\n"
                f"          <credentialsId>github-token</credentialsId>\n"
                f"          <repoOwner>{self.cfg.github_org}</repoOwner>\n"
                f"          <repository>{name}</repository>\n"
                f"          <traits>\n"
                f"            <org.jenkinsci.plugin.gitea.BranchDiscoveryTrait>\n"
                f"              <strategyId>1</strategyId>\n"
                f"            </org.jenkinsci.plugin.gitea.BranchDiscoveryTrait>\n"
                f"{branch_filter}"
                f"          </traits>\n"
                f"        </source>\n"
            )
        else:
            # github.com — use GitHubSCMSource for richer integration
            scm_block = (
                f"        <source class=\"org.jenkinsci.plugins.github_branch_source.GitHubSCMSource\">\n"
                f"          <repoOwner>{self.cfg.github_org}</repoOwner>\n"
                f"          <repository>{name}</repository>\n"
                f"          <credentialsId>github-token</credentialsId>\n"
                f"          <traits>\n"
                f"            <org.jenkinsci.plugins.github_branch_source.BranchDiscoveryTrait>\n"
                f"              <strategyId>1</strategyId>\n"
                f"            </org.jenkinsci.plugins.github_branch_source.BranchDiscoveryTrait>\n"
                f"{branch_filter}"
                f"          </traits>\n"
                f"        </source>\n"
            )
        strategy = (
            f"      <strategy class=\"jenkins.branch.DefaultBranchPropertyStrategy\">\n"
            f"        <properties class=\"empty-list\"/>\n"
            f"      </strategy>\n"
        )
        return (
            f"<?xml version='1.1' encoding='UTF-8'?>\n"
            f"<org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject>\n"
            f"  <displayName>{name}</displayName>\n"
            f"{scan_trigger}"
            f"  <sources class=\"jenkins.branch.MultiBranchProject$BranchSourceList\">\n"
            f"    <data>\n"
            f"      <jenkins.branch.BranchSource>\n"
            f"{scm_block}"
            f"{strategy}"
            f"      </jenkins.branch.BranchSource>\n"
            f"    </data>\n"
            f"  </sources>\n"
            f"  <factory class=\"org.jenkinsci.plugins.workflow.multibranch.WorkflowBranchProjectFactory\">\n"
            f"    <scriptPath>Jenkinsfile</scriptPath>\n"
            f"  </factory>\n"
            f"</org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject>"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # SCAFFOLD HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _scaffold(self, target: Path, name: str, template: str,
                  owner: str, description: str, ap3_hosted: bool = True):
        step(f"Scaffolding '{name}' from template '{template}'")
        tpl_root = self.cfg.templates_dir / template
        if not tpl_root.exists():
            error_exit(f"Template '{template}' not found at {tpl_root}")
        # New layout: source files live in src/, metadata (template.yaml, build.yaml) at root.
        # Legacy layout: all files at root (no src/ subdir).
        src_dir = tpl_root / "src"
        if src_dir.exists():
            copy_src = src_dir
        else:
            # Legacy: copy everything except template.yaml
            copy_src = tpl_root
        if not self.dry_run:
            if copy_src == tpl_root:
                shutil.copytree(copy_src, target,
                                ignore=shutil.ignore_patterns("template.yaml"))
            else:
                shutil.copytree(copy_src, target)
            # Copy build config into .platform/ so the shared lib can read it
            build_yaml = tpl_root / "build.yaml"
            if build_yaml.exists():
                platform_dir = target / ".platform"
                platform_dir.mkdir(exist_ok=True)
                shutil.copy2(build_yaml, platform_dir / "build.yaml")
            self._replace_placeholders(target, name, owner, description)
            ap3_dir = target / ".ap3"
            ap3_dir.mkdir(exist_ok=True)
            (ap3_dir / "hooks.yaml").write_text(
                _DEFAULT_HOOKS_YAML.format(ap3_hosted=str(ap3_hosted).lower())
            )
            bs = target / "bootstrap.sh"
            if bs.exists():
                set_executable(bs)

    def _replace_placeholders(self, path: Path, name, owner, description):
        lib_repo_url = self.cfg.resolved_shared_lib_url
        replacements = {
            "{{SERVICE_NAME}}":       name,
            "{{SERVICE_NAME_UPPER}}": name.upper().replace("-", "_"),
            "{{OWNER}}":              owner,
            "{{DESCRIPTION}}":        description,
            "{{YEAR}}":               str(datetime.now().year),
            "{{DATE}}":               datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "{{SHARED_LIB_VERSION}}": self.cfg.shared_lib_version,
            "{{LIB_REPO_URL}}":       lib_repo_url,
        }
        for f in path.rglob("*"):
            if f.is_file() and self._is_text_file(f):
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    for k, v in replacements.items():
                        content = content.replace(k, v)
                    f.write_text(content, encoding="utf-8")
                except (PermissionError, OSError):
                    pass
            if "SERVICE_NAME" in f.name:
                try:
                    f.rename(f.parent / f.name.replace("SERVICE_NAME", name))
                except OSError:
                    pass

    def _is_text_file(self, path: Path) -> bool:
        skip = {".jar", ".class", ".png", ".jpg", ".gif", ".ico",
                ".zip", ".exe", ".dll", ".so", ".dylib"}
        return path.suffix not in skip

    def _git_init(self, target: Path, name: str):
        step("Initialising git repository")
        if self.dry_run:
            return
        for cmd in [
            ["git", "init", "-b", "main"],
            ["git", "add", "."],
            ["git", "commit", "-m", f"chore: initial scaffold for {name}"],
            ["git", "checkout", "-b", "develop"],
            ["git", "checkout", "main"],
        ]:
            run(cmd, cwd=target, check=True, capture_output=True)

    def _register_in_service_catalog(
        self,
        name: str,
        owner: str,
        description: str,
        source_mode: str,
        template: str,
        repo_url: str,
        ap3_hosted: bool,
    ):
        step(f"Registering '{name}' in service catalog")
        if self.dry_run:
            return
        from identity import resolve_identity
        identity = resolve_identity(self.cfg)
        self.cfg.save_service(name, {
            "name":        name,
            "owner":       owner,
            "description": description,
            "source_mode": source_mode,
            "template":    template,
            "repo_url":    repo_url,
            "ap3_hosted":  ap3_hosted,
            "created_at":  datetime.now(timezone.utc).isoformat(),
            "created_by":  identity.display_name,
        })

    # ─────────────────────────────────────────────────────────────────────────
    # LIST / INFO
    # ─────────────────────────────────────────────────────────────────────────

    def remove(
        self,
        name: str,
        force: bool = False,
    ):
        """Remove a service from all environments, destroy its Jenkins pipeline,
        and remove any AP3/Jenkins webhooks from the GitHub repo (repo is kept)."""
        envs_with_service = self._envs_containing(name)
        in_catalog = self.cfg.service_catalog_path(name).exists()
        if not envs_with_service and not in_catalog:
            error_exit(f"Service '{name}' not found in catalog or any environment.")

        from identity import resolve_identity, format_disclaimer
        from output import confirm_with_actor
        identity = resolve_identity(self.cfg)
        self._require_tokens(identity, needs_github=in_catalog, needs_jenkins=in_catalog)

        actions = [
            f"Remove '{name}' from environments: {', '.join(envs_with_service)}",
            f"Git commit: 'svc: remove service {name}'",
            f"Delete Jenkins pipeline '{name}'",
            f"Remove AP3/Jenkins webhooks from "
            f"Git repo '{self.cfg.github_account}/{name}' (repo is kept)",
        ]

        if not self.dry_run and not self.json_output:
            confirm_with_actor(format_disclaimer(identity, actions), force=force)

        steps = []
        warnings = []

        def _warn(msg):
            warn(msg)
            warnings.append(msg)

        # ── Remove from every environment ─────────────────────────────────────
        for env in envs_with_service:
            try:
                data = self.cfg.load_versions(env)
                data.setdefault("services", {}).pop(name, None)
                if not self.dry_run:
                    self.cfg.save_versions(env, data)
                steps.append({"step": f"remove_from_env_{env}", "status": "ok"})
            except Exception as e:
                _warn(f"Could not update env '{env}': {e}")
                steps.append({"step": f"remove_from_env_{env}",
                               "status": "failed", "reason": str(e)})

        # ── Remove from service catalog ───────────────────────────────────────
        if not self.dry_run:
            removed_from_catalog = self.cfg.delete_service(name)
            steps.append({
                "step": "remove_from_catalog",
                "status": "ok" if removed_from_catalog else "skipped",
            })

        # ── Commit env + catalog changes ──────────────────────────────────────
        if not self.dry_run:
            commit_ok = self._git_commit(
                f"svc: remove service '{name}'",
                collect_warnings=warnings,
                stage_extra=["services/"],
            )
            steps.append({
                "step": "git_commit",
                "status": "ok" if commit_ok else "failed",
            })

        # ── Delete Jenkins pipeline ───────────────────────────────────────────
        try:
            self._delete_jenkins_pipeline(name)
            steps.append({"step": "jenkins_pipeline_delete", "status": "ok"})
        except Exception as e:
            _warn(f"Jenkins pipeline deletion failed: {e}")
            steps.append({"step": "jenkins_pipeline_delete",
                           "status": "failed", "reason": str(e)})

        # ── Remove GitHub webhooks ────────────────────────────────────────────
        try:
            removed = self._remove_jenkins_webhooks(name)
            steps.append({"step": "github_webhooks_remove",
                           "status": "ok", "removed": removed})
        except Exception as e:
            _warn(f"GitHub webhook removal failed: {e}")
            steps.append({"step": "github_webhooks_remove",
                           "status": "failed", "reason": str(e)})

        # ── Output ────────────────────────────────────────────────────────────
        result = {
            "service":  name,
            "envs":     envs_with_service,
            "steps":    steps,
            "warnings": warnings,
        }

        if self.json_output:
            print(json.dumps(result, indent=2))
        else:
            success(f"Service '{name}' removed.")
            print()
            for s in steps:
                icon = "+" if s["status"] == "ok" else ("~" if s["status"] == "skipped" else "x")
                detail = s.get("reason", "")
                print(f"  {icon}  {s['step']}" + (f"  — {detail}" if detail else ""))
            if warnings:
                print()
                for w in warnings:
                    print(f"  !  {w}")

        return result

    def list_services(self):
        services = self._collect_all_services()
        if self.json_output:
            print(json.dumps(services, indent=2))
            return
        if not services:
            out("No services found.")
            return
        col_w = [30, 14, 14, 14, 24]
        header = ["Service", "dev", "val", "prod", "Last deployed"]
        self._print_table(header, [
            [s["name"],
             s["versions"].get("dev", "—"),
             s["versions"].get("val", "—"),
             s["versions"].get("prod", "—"),
             s["last_deployed"]]
            for s in services
        ], col_w)

    def info(self, name):
        services = self._collect_all_services()
        svc = next((s for s in services if s["name"] == name), None)
        if not svc:
            error_exit(f"Service '{name}' not found in any environment.")

        repo_exists, repo_warning = self._check_repo_exists(svc.get("repo_url", ""))
        svc["repo_exists"] = repo_exists
        svc["repo_warning"] = repo_warning

        if self.json_output:
            print(json.dumps(svc, indent=2))
            return

        print(f"\n  Service: {name}")
        print(f"  {'─' * 40}")
        print(f"  owner         : {svc.get('owner', '—')}")
        print(f"  source mode   : {svc.get('source_mode', '—')}")
        if svc.get("repo_url"):
            status = "" if repo_exists else "  [NOT FOUND]" if repo_exists is False else "  [unreachable]"
            print(f"  repository    : {svc['repo_url']}{status}")
        if svc.get("template") and svc["template"] != "—":
            print(f"  template      : {svc['template']}")
        print()
        for env, ver in svc["versions"].items():
            print(f"  {env:<16} {ver}")
        print(f"\n  Last deployed : {svc['last_deployed']}")
        if repo_warning:
            print(f"\n  [WARNING] {repo_warning}")

    def _check_repo_exists(self, repo_url: str) -> tuple[bool | None, str]:
        """
        Check whether the repository URL is reachable.
        Returns (exists, warning_message).
          True,  ""          → accessible
          False, "..."       → 404 / repo gone
          None,  "..."       → can't determine (no URL, no token, timeout, etc.)
        """
        if not repo_url:
            return None, ""

        # Try GitHub REST API first (gives reliable 404 vs permission errors)
        import re as _re
        gh_match = _re.search(r"github[^/]*/([^/]+)/([^/]+?)(?:\.git)?$", repo_url)
        if gh_match and self.cfg.github_token:
            owner = gh_match.group(1)
            repo  = gh_match.group(2)
            api_base = self.cfg.github_api_base
            # Adjust for GitHub Enterprise; only skip cert verification for GHE
            host_match = _re.match(r"(https?://[^/]+)", repo_url)
            is_enterprise = host_match and "github.com" not in host_match.group(1)
            if is_enterprise:
                api_base = f"{host_match.group(1)}/api/v3"
            try:
                resp = requests.get(
                    f"{api_base}/repos/{owner}/{repo}",
                    headers={
                        "Authorization": f"token {self.cfg.github_token}",
                        "Accept": "application/vnd.github.v3+json",
                    },
                    timeout=8,
                    verify=not is_enterprise,
                )
                if resp.status_code == 200:
                    return True, ""
                if resp.status_code == 404:
                    return False, f"Repository not found: {repo_url}"
                if resp.status_code in (401, 403):
                    return None, f"Repository access denied (HTTP {resp.status_code}) — check GITHUB_TOKEN permissions"
                return None, f"GitHub API returned HTTP {resp.status_code}"
            except requests.exceptions.Timeout:
                return None, "Repository check timed out"
            except Exception as exc:
                return None, f"Repository check failed: {exc}"

        # Fallback: plain HEAD request (works for public repos without a token)
        host_match = _re.match(r"(https?://[^/]+)", repo_url)
        is_public_gh = host_match and "github.com" in host_match.group(1)
        try:
            resp = requests.head(repo_url.rstrip("/"), timeout=8,
                                 allow_redirects=True, verify=is_public_gh)
            if resp.status_code == 200:
                return True, ""
            if resp.status_code == 404:
                return False, f"Repository not found: {repo_url}"
            if resp.status_code in (401, 403):
                return None, f"Repository access denied (HTTP {resp.status_code})"
            # 301/302 already followed; anything else is ambiguous
            return None, f"Repository returned HTTP {resp.status_code}"
        except requests.exceptions.Timeout:
            return None, "Repository check timed out"
        except Exception as exc:
            return None, f"Repository check failed: {exc}"

    def _collect_all_services(self) -> list[dict]:
        service_map: dict[str, dict] = {}

        # Seed from the service catalog (source of truth for registered services)
        for svc_name in self.cfg.list_service_names():
            try:
                catalog = self.cfg.load_service(svc_name)
            except Exception:
                catalog = {}
            service_map[svc_name] = {
                "name":        svc_name,
                "owner":       catalog.get("owner", "—"),
                "template":    catalog.get("template", "—"),
                "source_mode": catalog.get("source_mode", "template"),
                "repo_url":    catalog.get("repo_url", ""),
                "ap3_hosted":  catalog.get("ap3_hosted", True),
                "versions":    {},
                "last_deployed": "—",
                "health": "unknown",
            }

        # Enrich with per-env version data (also picks up pre-catalog services)
        for env in self.cfg.list_envs():
            try:
                data = self.cfg.load_versions(env)
            except Exception:
                continue
            for svc_name, svc_data in (data.get("services") or {}).items():
                if svc_name not in service_map:
                    service_map[svc_name] = {
                        "name": svc_name, "owner": "—", "template": "—",
                        "source_mode": "unknown", "repo_url": "",
                        "ap3_hosted": False, "versions": {},
                        "last_deployed": "—", "health": "unknown",
                    }
                service_map[svc_name]["versions"][env] = svc_data.get("version", "—")
                if svc_data.get("deployed_at"):
                    service_map[svc_name]["last_deployed"] = svc_data["deployed_at"]

        return sorted(service_map.values(), key=lambda s: s["name"])

    def _build_actions(self, name, source_mode, template, fork_from,
                        external_repo_url, skip_jenkins, identity, ap3_hosted) -> list[str]:
        actions = []
        if source_mode == "template":
            actions.append(f"Scaffold '{name}' from template '{template}'")
            actions.append(f"Create GitHub repo {self.cfg.github_org}/{name}")
            actions.append("Set branch protection on main + develop")
        elif source_mode == "fork":
            actions.append(f"Clone {self.cfg.github_org}/{fork_from}")
            actions.append(f"Create GitHub repo {self.cfg.github_org}/{name} (from fork)")
        elif source_mode == "external":
            actions.append(f"Register external repo: {external_repo_url}")
        if not skip_jenkins:
            actions.append(f"Register Jenkins pipeline '{name}'")
        else:
            actions.append("Register Jenkins pipeline — skipped (--skip-jenkins)")
        actions.append(f"Register '{name}' in dev versions.yaml")
        return actions

    def _delete_jenkins_pipeline(self, name: str):
        step(f"Deleting Jenkins pipeline '{name}'")
        if self.dry_run:
            return
        resp = requests.post(
            f"{self.cfg.jenkins_url}/job/{name}/doDelete",
            auth=(self.cfg.jenkins_user, self.cfg.jenkins_token),
            timeout=10,
        )
        # 302 = redirect after success, 404 = already gone — both are fine
        if resp.status_code not in (200, 302, 404):
            raise RuntimeError(
                f"Jenkins API {resp.status_code}: {resp.text[:300]}"
            )

    def _remove_jenkins_webhooks(self, name: str) -> int:
        """Delete webhooks pointing to this Jenkins instance from the GitHub repo.
        Returns the number of webhooks removed."""
        step(f"Removing Jenkins webhooks from '{self.cfg.github_account}/{name}'")
        if self.dry_run:
            return 0
        api = self.cfg.github_api_base
        account = self.cfg.github_account
        headers = {
            "Authorization": f"token {self.cfg.github_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        resp = requests.get(
            f"{api}/repos/{account}/{name}/hooks",
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 404:
            # External repo or repo not under our org — nothing to remove
            return 0
        if resp.status_code != 200:
            raise RuntimeError(f"GitHub API {resp.status_code}: {resp.text[:200]}")

        jenkins_base = self.cfg.jenkins_url.rstrip("/")
        removed = 0
        for hook in resp.json():
            hook_url = hook.get("config", {}).get("url", "")
            if jenkins_base in hook_url:
                del_resp = requests.delete(
                    f"{api}/repos/{account}/{name}/hooks/{hook['id']}",
                    headers=headers,
                    timeout=10,
                )
                if del_resp.status_code in (204, 404):
                    removed += 1
        return removed

    def _envs_containing(self, name: str) -> list[str]:
        """Return names of all environments that have this service registered."""
        result = []
        for env in self.cfg.list_envs():
            try:
                data = self.cfg.load_versions(env)
                if name in (data.get("services") or {}):
                    result.append(env)
            except Exception:
                pass
        return result

    def _git_commit(self, message: str, collect_warnings: list = None,
                    stage_extra: list = None) -> bool:
        """Stage envs/ (+ any extra paths) and commit to the platform repo."""
        def _warn(msg):
            if collect_warnings is not None:
                collect_warnings.append(msg)
            else:
                warn(msg)

        try:
            run(["git", "rev-parse", "--git-dir"],
                cwd=self.cfg.root, check=True, capture_output=True)
        except Exception:
            _warn("Not a git repository — change not committed.")
            return False

        try:
            paths = ["envs/"] + (stage_extra or [])
            run(["git", "add", *paths],
                cwd=self.cfg.root, check=True, capture_output=True)
            run(["git", "commit", "-m", message],
                cwd=self.cfg.root, check=True, capture_output=True)
        except subprocess.CalledProcessError:
            _warn("Could not auto-commit. Run: git add envs/ services/ && git commit -m '" + message + "'")
            return False

        try:
            has_remote = run(
                ["git", "remote"], cwd=self.cfg.root,
                capture_output=True, text=True,
            ).stdout.strip()
            if not has_remote:
                return True
            branch = run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.cfg.root, capture_output=True, text=True,
            ).stdout.strip()
            run(["git", "pull", "--rebase", "origin", branch],
                cwd=self.cfg.root, check=True, capture_output=True)
            run(["git", "push", "origin", branch],
                cwd=self.cfg.root, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode(errors="replace").strip() if e.stderr else str(e)
            _warn(f"Could not push to remote: {stderr}. Push manually: git push origin {branch}")

        return True

    def _print_table(self, header, rows, col_widths):
        def fmt(row):
            return "  " + "  ".join(str(v).ljust(w) for v, w in zip(row, col_widths))
        sep = "  " + "  ".join("-" * w for w in col_widths)
        print()
        print(fmt(header))
        print(sep)
        for row in rows:
            print(fmt(row))
        print()

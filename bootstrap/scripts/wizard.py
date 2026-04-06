"""
wizard.py — Interactive bootstrap wizard for the AP3 platform.

Guides the user through creating their initial fixed environments
(production, validation, dev) and any additional environments they need.

Usage:
    python scripts/wizard.py                           # interactive
    python scripts/wizard.py --demo                    # seed with realistic example data
    python scripts/wizard.py --yes                     # skip confirmations (CI mode)
    python scripts/wizard.py --config bootstrap.yaml   # config-file mode (non-interactive)
"""

from __future__ import annotations

import argparse
import sys
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Make scripts/ importable when run directly
sys.path.insert(0, os.path.dirname(__file__))

from config import PlatformConfig, PLATFORMS
from output import success, step, warn, out


# ── Terminal helpers ───────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
BLUE   = "\033[34m"
TEAL   = "\033[36m"
AMBER  = "\033[33m"
GREEN  = "\033[32m"
MUTED  = "\033[90m"
CORAL  = "\033[31m"


def _c(color: str, text: str) -> str:
    """Apply ANSI color only when stdout is a tty."""
    if sys.stdout.isatty():
        return f"{color}{text}{RESET}"
    return text


def header(text: str):
    print()
    print(_c(BOLD, f"  {text}"))
    print(_c(MUTED, f"  {'─' * len(text)}"))


# ── Global state ──────────────────────────────────────────────────────────────
# _YES_MODE: skip interactive prompts (--yes or --config mode)
# _CONFIG:   answers loaded from --config YAML file; empty in interactive mode
# _GITHUB_LOGIN: resolved during _validate_tokens(), used for git push auth

_YES_MODE     = False
_CONFIG: dict = {}
_CONFIG_PATH  = ""
_GITHUB_LOGIN = ""   # set by _validate_tokens() for use in push credential embedding
_SENTINEL     = object()


def _cfg(key: str, default=_SENTINEL):
    """
    In config-file mode: return _CONFIG[key] (str), or 'default' if absent.
    In interactive mode: return 'default' unchanged (so callers use it as ask() default).
    Raises KeyError with a helpful message if key is missing and no default was given.
    """
    if not _CONFIG:
        return default if default is not _SENTINEL else ""
    if key in _CONFIG:
        val = _CONFIG[key]
        return str(val) if val is not None else ""
    if default is not _SENTINEL:
        return str(default) if default is not None else ""
    raise KeyError(
        f"Bootstrap config file '{_CONFIG_PATH}' is missing required key '{key}'.\n"
        f"  See testenv/bootstrap-config.yaml for the full schema."
    )


def _load_config(path: str) -> dict:
    """Load and minimally validate the bootstrap config YAML file."""
    import yaml
    p = Path(path)
    if not p.exists():
        print(f"\n  [error] Config file not found: {path}")
        sys.exit(1)
    with open(p) as f:
        data = yaml.safe_load(f) or {}
    required_top = {
        "github_url", "github_account_type", "github_org",
        "jenkins_url", "jenkins_git_url",
        "platform", "cluster_prefix", "environments",
    }
    missing = required_top - data.keys()
    if missing:
        print(f"\n  [error] Config file '{path}' missing required keys: {sorted(missing)}")
        sys.exit(1)
    required_envs = {"prod", "val", "dev"}
    envs = data.get("environments", {})
    missing_envs = required_envs - envs.keys()
    if missing_envs:
        print(f"\n  [error] Config file '{path}' missing required environments: {sorted(missing_envs)}")
        sys.exit(1)
    return data


def _validate_tokens(cfg: PlatformConfig):
    """
    Validate GitHub/Gitea and Jenkins tokens against their respective APIs.
    Called only in config-file mode. Exits with a clear message on missing or
    invalid tokens. Sets _GITHUB_LOGIN for use in subsequent git push auth.
    """
    import requests

    global _GITHUB_LOGIN

    header("Token validation")
    any_fatal = False

    # ── GitHub / Gitea ────────────────────────────────────────────────────────
    github_token = cfg.github_token
    if not github_token:
        print(f"\n  [error] Environment variable '{cfg.github_token_env}' is not set.")
        print( "          Source your credentials file and retry.")
        any_fatal = True
    else:
        try:
            resp = requests.get(
                f"{cfg.github_api_base}/user",
                headers={"Authorization": f"token {github_token}",
                         "Accept": "application/vnd.github.v3+json"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                _GITHUB_LOGIN = data.get("login", "")
                label = data.get("name") or _GITHUB_LOGIN or "unknown"
                success(f"Git hosting token valid  (user: {label})")
            elif resp.status_code == 401:
                print(f"\n  [error] {cfg.github_token_env} is invalid (HTTP 401). "
                      "Verify the token has the required scopes.")
                any_fatal = True
            else:
                warn(f"Git hosting token check returned HTTP {resp.status_code} — continuing.")
        except Exception as e:
            warn(f"Could not verify git hosting token ({e}) — continuing.")

    # ── Jenkins ───────────────────────────────────────────────────────────────
    jenkins_token = cfg.jenkins_token
    jenkins_user  = cfg.jenkins_user
    if not jenkins_token or not jenkins_user:
        missing = [v for v, val in [
            (cfg.jenkins_user_env, jenkins_user),
            (cfg.jenkins_token_env, jenkins_token),
        ] if not val]
        print(f"\n  [error] Environment variable(s) not set: {', '.join(missing)}")
        print( "          Source your credentials file and retry.")
        any_fatal = True
    else:
        try:
            resp = requests.get(
                f"{cfg.jenkins_url}/me/api/json",
                auth=(jenkins_user, jenkins_token),
                timeout=10,
            )
            if resp.status_code == 200:
                name = resp.json().get("fullName", jenkins_user)
                success(f"Jenkins token valid  (user: {name})")
            elif resp.status_code == 401:
                print(f"\n  [error] Jenkins token is invalid (HTTP 401). "
                      "Verify the credentials and retry.")
                any_fatal = True
            else:
                warn(f"Jenkins token check returned HTTP {resp.status_code} — continuing.")
        except Exception as e:
            warn(f"Could not verify Jenkins token ({e}) — continuing.")

    if any_fatal:
        print()
        print("  Fix the errors above and re-run bootstrap.")
        sys.exit(1)


def ask(prompt: str, default: str = "", choices: list[str] | None = None) -> str:
    """Prompt for a string value, with optional default and validation."""
    if _YES_MODE:
        value = default or (choices[0] if choices else "")
        print(f"  {prompt}: {value}  (auto)")
        return value
    hint = ""
    if choices:
        hint = f"  ({'/'.join(choices)}) "
    elif default:
        hint = f"  [{default}] "
    while True:
        try:
            raw = input(f"\n  {prompt}{hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(0)
        value = raw or default
        if not value:
            print(f"  {_c(CORAL, '! Required — please enter a value.')}")
            continue
        if choices and value not in choices:
            opts = ", ".join(choices)
            print(f"  {_c(CORAL, '! Must be one of: ' + opts)}")
            continue
        return value


def ask_optional(prompt: str, default: str = "") -> str:
    """Prompt for an optional string value."""
    if _YES_MODE:
        print(f"  {prompt}: {default or '(empty)'}  (auto)")
        return default
    hint = f"  [{default or 'leave empty to skip'}] "
    try:
        raw = input(f"\n  {prompt}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        sys.exit(0)
    return raw or default


def ask_yes(prompt: str, default: bool = True) -> bool:
    if _YES_MODE:
        print(f"  {prompt}: {'yes' if default else 'no'}  (auto)")
        return default
    hint = "[Y/n]" if default else "[y/N]"
    try:
        raw = input(f"\n  {prompt} {hint}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        sys.exit(0)
    if not raw:
        return default
    return raw in ("y", "yes")


def hr():
    print()
    print(_c(MUTED, "  " + "─" * 56))


# ── Env definition data class ─────────────────────────────────────────────────

class EnvDef:
    """Configuration for one environment to create."""
    def __init__(self, name: str, role: str,
                 platform: str = "openshift",
                 cluster: str = "",
                 namespace: str = "",
                 description: str = ""):
        self.name        = name
        self.role        = role          # "prod" | "val" | "dev" | "custom"
        self.platform    = platform
        self.cluster     = cluster       # may be filled in interactively
        self.namespace   = namespace     # empty = auto-generated
        self.description = description


# ── Wizard steps ──────────────────────────────────────────────────────────────

def _welcome():
    print()
    print(_c(BOLD + BLUE, "  ╔══════════════════════════════════════════════╗"))
    print(_c(BOLD + BLUE, "  ║        AP3 Platform — Bootstrap Wizard       ║"))
    print(_c(BOLD + BLUE, "  ╚══════════════════════════════════════════════╝"))
    print()
    print("  This wizard will configure your source control, CI/CD,")
    print("  and create your standard fixed environments.")
    print()
    print(_c(MUTED, "  Tip: press Enter to accept the [default] value."))
    print(_c(MUTED, "  Tip: all settings can be changed later in platform.yaml."))


def _collect_integrations(cfg: PlatformConfig):
    """
    Step 0 — capture GitHub/Gitea and Jenkins coordinates.
    Written to platform.yaml and used for all service creation and CI/CD.
    In config-file mode all values come from _CONFIG; no prompts are shown.
    """
    import yaml as _yaml

    platform_file = cfg.root / "platform.yaml"
    with open(platform_file) as f:
        data = _yaml.safe_load(f) or {}

    if _CONFIG:
        # ── Config-file mode: read all values from _CONFIG ────────────────────
        github_url      = _cfg("github_url",       data.get("github_url", "https://github.com"))
        account_type    = _cfg("github_account_type", data.get("github_account_type", "org"))
        github_org      = _cfg("github_org",        data.get("github_org", "my-org"))
        jenkins_url     = _cfg("jenkins_url",       data.get("jenkins_url", "https://jenkins.internal"))
        github_api_path = _cfg("github_api_path",   data.get("github_api_path", ""))
        jenkins_git_url = _cfg("jenkins_git_url",   data.get("jenkins_git_url", ""))
        jenkins_hook_url = _cfg("jenkins_hook_url", data.get("jenkins_hook_url", ""))
    else:
        # ── Interactive mode ──────────────────────────────────────────────────
        header("Step 0 — Source control & CI/CD")
        print()
        print("  These settings are used when creating new services.")
        print("  Press Enter to keep the current value, or type a new one.")
        print(_c(MUTED, "  You can also skip this step and edit platform.yaml directly."))

        github_url = ask_optional(
            "GitHub URL (leave as-is for github.com, or enter Enterprise/Gitea URL)",
            default=data.get("github_url", "https://github.com"),
        ) or "https://github.com"

        account_type = ask(
            "GitHub account type",
            default=data.get("github_account_type", "org"),
            choices=["org", "user"],
        )

        github_org = ask_optional(
            f"GitHub {'organisation name' if account_type == 'org' else 'username'}",
            default=data.get("github_org", "my-org"),
        ) or "my-org"

        jenkins_url = ask_optional(
            "Jenkins URL",
            default=data.get("jenkins_url", "https://jenkins.internal"),
        ) or "https://jenkins.internal"

        # API path: only relevant for non-github.com hosts
        if "github.com" not in github_url:
            github_api_path = ask_optional(
                "API path for this Git host  (e.g. 'api/v1' for Gitea, blank for GitHub Enterprise)",
                default=data.get("github_api_path", ""),
            )
        else:
            github_api_path = ""

        jenkins_git_url = ask_optional(
            "URL Jenkins uses internally to clone repos  "
            "(leave blank if same as GitHub URL — only differs when Jenkins is in Docker)",
            default=data.get("jenkins_git_url", ""),
        )

        jenkins_hook_url = ask_optional(
            "URL the git server uses to reach Jenkins for webhooks  "
            "(leave blank if same as Jenkins URL — only differs when both are in Docker)",
            default=data.get("jenkins_hook_url", ""),
        )

    # ── Write to platform.yaml if anything changed ────────────────────────────
    updates = {
        "github_url":          github_url,
        "github_account_type": account_type,
        "github_org":          github_org,
        "jenkins_url":         jenkins_url,
        "github_api_path":     github_api_path,
        "jenkins_git_url":     jenkins_git_url,
        "jenkins_hook_url":    jenkins_hook_url,
    }
    changed = any(data.get(k) != v for k, v in updates.items())

    if changed:
        data.update(updates)
        with open(platform_file, "w") as f:
            _yaml.dump(data, f, default_flow_style=False, allow_unicode=True,
                       sort_keys=False)
        for k, v in updates.items():
            setattr(cfg, k, v)
        step(
            f"platform.yaml updated  "
            f"(github={github_url}/{account_type}/{github_org}  "
            f"jenkins={jenkins_url})"
        )
    else:
        out("No changes to integration settings.")


def _collect_cluster_details(
    platform: str,
    cluster_name: str,
    cfg: PlatformConfig,
    env_config: dict | None = None,
) -> "ClusterProfile | None":
    """
    Ask for the connection details of one cluster and register it immediately
    in platform.yaml. Returns None if the cluster already exists and user
    chooses to reuse it.

    env_config: when set (config-file mode), build the ClusterProfile directly
    from this dict without prompting.
    """
    from config import ClusterProfile

    if cluster_name in cfg.clusters:
        existing = cfg.get_cluster_profile(cluster_name)
        print(f"  {_c(MUTED, f'Cluster \"{cluster_name}\" already exists ({existing.platform}) — reusing it.')}")
        return existing

    registry_default = cfg.registries.get(platform, cfg.registry)

    if env_config is not None:
        # ── Config-file mode: build profile from provided dict ─────────────────
        suffix = cluster_name.split("-")[-1]
        if platform == "openshift":
            profile = ClusterProfile(
                name=cluster_name, platform=platform,
                registry=env_config.get("registry", registry_default),
                helm_values_suffix=suffix,
                api_url=env_config.get("api_url", ""),
                context=env_config.get("context", cluster_name),
            )
        else:  # aws
            profile = ClusterProfile(
                name=cluster_name, platform=platform,
                registry=env_config.get("registry", registry_default),
                helm_values_suffix=suffix,
                region=env_config.get("region", "eu-west-1"),
                cluster_name=env_config.get("cluster_name", cluster_name),
            )
        cfg.save_cluster(profile)
        step(f"Cluster \"{cluster_name}\" registered in platform.yaml")
        return profile

    print()
    print(f"  {_c(BOLD, 'New cluster:')} {_c(TEAL, cluster_name)} ({platform})")
    print()

    if platform == "openshift":
        api_url = ask_optional(
            "  OpenShift API URL (e.g. https://api.cluster.example.com:6443)",
            default=f"https://api.{cluster_name}.internal:6443",
        )
        context = ask_optional(
            "  kubeconfig context name",
            default=cluster_name,
        )
        registry = ask_optional(
            "  Container registry",
            default=registry_default,
        ) or registry_default
        suffix = cluster_name.split("-")[-1]
        profile = ClusterProfile(
            name=cluster_name, platform=platform,
            registry=registry, helm_values_suffix=suffix,
            api_url=api_url, context=context,
        )
    else:  # aws
        region = ask_optional(
            "  AWS region",
            default="eu-west-1",
        ) or "eu-west-1"
        eks_name = ask_optional(
            "  EKS cluster name (used with aws eks update-kubeconfig)",
            default=cluster_name,
        ) or cluster_name
        registry = ask_optional(
            "  ECR registry URL",
            default=registry_default,
        ) or registry_default
        suffix = cluster_name.split("-")[-1]
        profile = ClusterProfile(
            name=cluster_name, platform=platform,
            registry=registry, helm_values_suffix=suffix,
            region=region, cluster_name=eks_name,
        )

    cfg.save_cluster(profile)
    print(f"  {_c(GREEN, f'✓ Cluster \"{cluster_name}\" registered in platform.yaml')}")
    return profile


def _collect_platform_defaults(cfg: PlatformConfig) -> tuple[str, str]:
    """Ask once for the default platform and cluster prefix."""
    if _CONFIG:
        # Config-file mode: read directly, skip prompts
        platform       = _cfg("platform", "openshift")
        cluster_prefix = _cfg("cluster_prefix", "openshift")
        step(f"Platform: {platform}  /  cluster prefix: {cluster_prefix}")
        return platform, cluster_prefix

    header("Step 1 — Default platform")
    print()
    print("  AP3 supports OpenShift (current) and AWS/EKS (hybrid target).")
    print("  You can override per environment if you use both.")

    platform = ask(
        "Default platform for your environments",
        default="openshift",
        choices=["openshift", "aws"],
    )

    header("Step 2 — Cluster naming")
    print()
    if platform == "openshift":
        print("  Each environment runs in its own OpenShift cluster (or namespace).")
        print("  Convention: openshift-dev, openshift-val, openshift-prod")
        cluster_prefix = ask_optional(
            "Cluster name prefix",
            default="openshift",
        ) or "openshift"
    else:
        print("  Each environment runs in an EKS cluster.")
        print("  Convention: platform-eks-dev, platform-eks-prod")
        cluster_prefix = ask_optional(
            "Cluster name prefix",
            default="platform-eks",
        ) or "platform-eks"

    print()
    print(_c(MUTED, f"  Clusters will be named: {cluster_prefix}-dev, {cluster_prefix}-val, {cluster_prefix}-prod"))
    print(_c(MUTED,  "  You will be asked for connection details for each one."))

    return platform, cluster_prefix


def _collect_environments(platform: str, cluster_prefix: str,
                           cfg: PlatformConfig) -> list[EnvDef]:
    """
    Ask about each standard environment and create its cluster profile.
    Default set: prod, val, dev.  No staging by default.
    In config-file mode, read all values from _CONFIG["environments"].
    """
    if _CONFIG:
        # ── Config-file mode ──────────────────────────────────────────────────
        step("Configuring standard environments (prod / val / dev)")
        env_defs: list[EnvDef] = []
        role_order = [("prod", "prod"), ("val", "val"), ("dev", "dev")]
        for role, default_name in role_order:
            ec = _CONFIG["environments"][role]
            name         = ec.get("name", default_name)
            cluster_name = ec.get("cluster", f"{cluster_prefix}-{role}")
            namespace    = ec.get("namespace", f"platform-{name}")
            description  = ec.get("description",
                                   {"prod": "Production environment — manual approval required",
                                    "val":  "Validation environment — pre-production QA",
                                    "dev":  "Development environment — auto-deployed on commit"
                                   }.get(role, ""))
            profile = _collect_cluster_details(platform, cluster_name, cfg,
                                               env_config=ec)
            d = EnvDef(name=name, role=role, platform=platform,
                       cluster=cluster_name, namespace=namespace,
                       description=description)
            if profile:
                d.platform = profile.platform
            env_defs.append(d)
        return env_defs

    # ── Interactive mode ──────────────────────────────────────────────────────
    header("Step 3 — Standard environments & clusters")
    print()
    print("  AP3 recommends three fixed environments:")
    print()
    print(f"    {_c(CORAL, 'prod')}  — production  (protected, manual approval gate)")
    print(f"    {_c(AMBER, 'val')}   — validation  (pre-production QA / UAT)")
    print(f"    {_c(TEAL,  'dev')}   — development (auto-deploy on commit)")
    print()
    print("  For each environment you will:")
    print("  1. Confirm the environment name")
    print("  2. Confirm/adjust the cluster name")
    print("  3. Provide cluster connection details (creates the profile)")
    print("  4. Optionally provide a pre-existing namespace")

    defaults = [
        EnvDef("prod", "prod", platform, f"{cluster_prefix}-prod",
               description="Production environment — manual approval required"),
        EnvDef("val",  "val",  platform, f"{cluster_prefix}-val",
               description="Validation environment — pre-production QA"),
        EnvDef("dev",  "dev",  platform, f"{cluster_prefix}-dev",
               description="Development environment — auto-deployed on commit"),
    ]

    env_defs = []
    for d in defaults:
        hr()
        print()
        print(f"  {_c(BOLD, d.role.upper())} environment")

        # 1. Env name
        name = ask("  Environment name", default=d.name)
        d.name = name

        # 2. Cluster name
        cluster_name = ask("  Cluster name", default=d.cluster)
        d.cluster = cluster_name

        # 3. Create/confirm cluster profile
        profile = _collect_cluster_details(d.platform, cluster_name, cfg)
        if profile:
            d.platform = profile.platform  # authoritative from profile

        # 4. Namespace
        ns = ask_optional(
            "  Namespace (leave empty to auto-generate 'platform-{name}')",
            default="",
        )
        d.namespace = ns if ns else f"platform-{name}"

        env_defs.append(d)

    return env_defs


def _collect_extra_environments(platform: str, cluster_prefix: str,
                                  cfg: PlatformConfig) -> list[EnvDef]:
    """Optionally add more fixed environments beyond the standard three."""
    if _CONFIG:
        # ── Config-file mode ──────────────────────────────────────────────────
        extra_list = _CONFIG.get("extra_environments") or []
        if not extra_list:
            return []
        extras: list[EnvDef] = []
        for ec in extra_list:
            name         = ec.get("name", "extra")
            plat         = ec.get("platform", platform)
            cluster_name = ec.get("cluster", f"{cluster_prefix}-{name}")
            namespace    = ec.get("namespace", f"platform-{name}")
            desc         = ec.get("description", "")
            profile      = _collect_cluster_details(plat, cluster_name, cfg,
                                                    env_config=ec)
            d = EnvDef(name=name, role="custom", platform=plat,
                       cluster=cluster_name, namespace=namespace, description=desc)
            if profile:
                d.platform = profile.platform
            extras.append(d)
        return extras

    # ── Interactive mode ──────────────────────────────────────────────────────
    hr()
    print()
    if not ask_yes("Add more fixed environments? (staging, uat, demo, …)", default=False):
        return []

    extras = []
    while True:
        print()
        name        = ask("  Environment name (e.g. 'staging', 'uat', 'demo')")
        plat        = ask("  Platform", default=platform, choices=["openshift", "aws"])
        cluster_name = ask("  Cluster name", default=f"{cluster_prefix}-{name}")

        profile = _collect_cluster_details(plat, cluster_name, cfg)

        ns   = ask_optional("  Namespace (leave empty to auto-generate)", default="")
        desc = ask_optional("  Description", default="")

        extras.append(EnvDef(
            name=name, role="custom",
            platform=plat, cluster=cluster_name,
            namespace=ns or f"platform-{name}",
            description=desc,
        ))

        if not ask_yes("  Add another environment?", default=False):
            break

    return extras


def _confirm_plan(env_defs: list[EnvDef], demo: bool) -> bool:
    """Show a summary of what will be created and ask for confirmation."""
    hr()
    header("Confirmation")
    print()
    print("  The following environments will be created:\n")

    col_w = [12, 12, 18, 30]
    header_row = ["Name", "Platform", "Cluster", "Namespace"]
    print("  " + "  ".join(h.ljust(w) for h, w in zip(header_row, col_w)))
    print("  " + "  ".join("─" * w for w in col_w))
    for d in env_defs:
        print("  " + "  ".join(v.ljust(w) for v, w in zip(
            [d.name, d.platform, d.cluster, d.namespace], col_w
        )))

    if demo:
        print()
        print(_c(AMBER, "  --demo flag set: example service data will be seeded"))
        print(_c(MUTED, "  (clearly marked as demo — will not appear in history)"))

    print()
    return ask_yes("Proceed with environment creation?", default=True)


# ── Env creation ──────────────────────────────────────────────────────────────

def _create_environments(cfg: PlatformConfig, env_defs: list[EnvDef]):
    """Write versions.yaml for each environment."""
    import yaml

    for d in env_defs:
        env_path = cfg.env_path(d.name)
        env_path.mkdir(parents=True, exist_ok=True)
        versions_path = cfg.env_versions_path(d.name)

        if versions_path.exists():
            warn(f"Environment '{d.name}' already exists — skipping.")
            continue

        meta = {
            "env_type":    "fixed",
            "platform":    d.platform,
            "cluster":     d.cluster,
            "registry":    cfg.registries.get(d.platform, cfg.registry),
            "namespace":   d.namespace,
            "description": d.description,
            "commit":      "wizard",      # distinct from "bootstrap" — not a stub
        }

        data = {"_meta": meta, "services": {}}
        comment = (
            f"# envs/{d.name}/versions.yaml\n"
            f"# Source of truth for what runs in the {d.name} environment.\n"
            f"# Updated automatically on each deploy — do not edit manually.\n"
            f"# Use: python scripts/platform_cli.py deploy "
            f"--env {d.name} --service <n> --version <ver>\n\n"
        )
        with open(versions_path, "w") as f:
            f.write(comment)
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True,
                      sort_keys=False)

        step(f"Created environment '{d.name}'  [{d.platform} / {d.cluster}]")


def _remove_stub_envs(cfg: PlatformConfig, created_names: list[str]):
    """
    Remove the placeholder stub envs (dev, staging, prod) that were
    committed with the repo template if the wizard is creating real ones.
    Only stubs with commit == 'bootstrap' and empty services are removed.
    """
    import shutil, yaml

    stubs = ["dev", "staging", "prod"]
    for stub in stubs:
        if stub in created_names:
            continue   # user kept/renamed this one — leave it
        path = cfg.env_versions_path(stub)
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except Exception:
            continue
        meta = data.get("_meta", {})
        services = data.get("services", {})
        if meta.get("commit") == "bootstrap" and not services:
            shutil.rmtree(cfg.env_path(stub))
            out(f"Removed template stub '{stub}' (replaced by wizard environments)")


# ── Demo data seeding ─────────────────────────────────────────────────────────

def _seed_demo_data(cfg: PlatformConfig, env_defs: list[EnvDef]):
    """
    Seed realistic but clearly-labelled example service data.
    Uses the actual cluster names from env_defs (created during wizard),
    so the dashboard shows real cluster references — not hardcoded "openshift-dev".
    """
    import yaml
    from datetime import datetime, timezone, timedelta

    step("Seeding demo service data")

    # Build per-env demo service templates
    DEMO_TEMPLATES = {
        "prod": {
            "spe":            {"version": "1.4.0",         "health": "healthy"},
            "service-auth":   {"version": "2.2.1",         "health": "healthy"},
            "service-orders": {"version": "1.8.5",         "health": "healthy"},
            "lib-platform":   {"version": "1.3.0",         "health": "healthy"},
        },
        "val": {
            "spe":            {"version": "1.5.0-RC1",     "health": "healthy"},
            "service-auth":   {"version": "2.3.0",         "health": "healthy"},
            "service-orders": {"version": "1.9.0",         "health": "healthy"},
            "lib-platform":   {"version": "1.4.0",         "health": "healthy"},
        },
        "dev": {
            "spe":            {"version": "1.5.0-SNAPSHOT", "health": "unknown"},
            "service-auth":   {"version": "2.4.0-SNAPSHOT", "health": "unknown"},
            "service-orders": {"version": "1.9.0-SNAPSHOT", "health": "unknown"},
            "lib-platform":   {"version": "1.4.0-SNAPSHOT", "health": "unknown"},
        },
    }

    now = datetime.now(timezone.utc)

    for d in env_defs:
        # Map wizard env to demo template by role
        demo_key = d.role if d.role in DEMO_TEMPLATES else (
            "prod" if "prod" in d.name else
            "val"  if any(x in d.name for x in ("val", "stag", "uat", "qa")) else
            "dev"
        )
        template = DEMO_TEMPLATES.get(demo_key, DEMO_TEMPLATES["dev"])

        path = cfg.env_versions_path(d.name)
        if not path.exists():
            continue

        data = yaml.safe_load(path.read_text()) or {}
        registry = data.get("_meta", {}).get("registry", cfg.registry)

        # Age offsets: prod=14 days ago, val=7 days ago, dev=yesterday
        age_days = {"prod": 14, "val": 7, "dev": 1}.get(demo_key, 3)

        services = {}
        for i, (svc_name, svc_info) in enumerate(template.items()):
            version = svc_info["version"]
            deployed_at = (now - timedelta(days=age_days - i * 0.5)).isoformat()
            services[svc_name] = {
                "version":     version,
                "image":       f"{registry}/{svc_name}:{version}",
                "deployed_at": deployed_at,
                "deployed_by": "demo-wizard <demo@example.com>",
                "health":      svc_info["health"],
                "demo":        True,
            }

        data["services"]               = services
        data["_meta"]["commit"]        = "demo"
        data["_meta"]["updated_at"]    = now.isoformat()
        data["_meta"]["updated_by"]    = "demo-wizard"

        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True,
                      sort_keys=False)

        success(
            f"Seeded demo data for '{d.name}' "
            f"({len(services)} services @ cluster {d.cluster})"
        )


# ── Update platform.yaml defaults ─────────────────────────────────────────────

def _update_platform_yaml_defaults(cfg: PlatformConfig, env_defs: list[EnvDef]):
    """
    Update platform.yaml default_cluster_* keys to match the wizard choices.
    Cluster profiles are already saved during _collect_cluster_details().
    """
    import yaml as _yaml

    platform_file = cfg.root / "platform.yaml"
    if not platform_file.exists():
        return

    with open(platform_file) as f:
        data = _yaml.safe_load(f) or {}

    for d in env_defs:
        if d.role == "prod":
            data["default_cluster_prod"] = d.cluster
        elif d.role in ("val", "staging"):
            data["default_cluster_staging"] = d.cluster
        elif d.role == "dev":
            data["default_cluster_dev"]  = d.cluster
            data["default_cluster_poc"]  = d.cluster  # POCs on dev cluster

    with open(platform_file, "w") as f:
        _yaml.dump(data, f, default_flow_style=False, allow_unicode=True,
                   sort_keys=False)

    step("Updated platform.yaml default cluster pointers")


# ── Summary ───────────────────────────────────────────────────────────────────

def _print_summary(env_defs: list[EnvDef], demo: bool):
    hr()
    print()
    print(_c(GREEN + BOLD, "  ✓ AP3 platform bootstrapped successfully!"))
    print()
    print("  Environments created:")
    for d in env_defs:
        print(f"    {_c(TEAL, d.name):<20} {d.platform}  /  {d.cluster}")
    if demo:
        print()
        print(_c(AMBER, "  Demo data seeded. Use 'env list' and 'history' to explore."))
    print()
    print("  Next steps:")
    print()
    print("    make dev                          # Start API + dashboard")
    print("    python scripts/platform_cli.py env list")
    print("    python scripts/platform_cli.py history")
    print("    python scripts/platform_cli.py service create \\")
    print("        --name my-svc --template springboot --owner team-x")
    print()


# ── Infrastructure setup (config-file mode only) ──────────────────────────────

def _create_platform_repo(cfg: PlatformConfig, repo_name: str):
    """
    Create the platform repo in Gitea/GitHub, configure git remote 'origin'
    with embedded credentials so bootstrap.sh's subsequent push succeeds.
    Skipped if the remote already exists AND the remote repo is reachable.
    """
    import subprocess, requests

    step(f"Creating platform repo '{cfg.github_org}/{repo_name}' in git hosting")

    token = cfg.github_token
    login = _GITHUB_LOGIN or cfg.github_org
    base  = cfg.github_url.rstrip("/")

    # Build push URL with embedded credentials (http or https)
    if base.startswith("https://"):
        push_url  = f"https://{login}:{token}@{base[8:]}/{cfg.github_org}/{repo_name}.git"
    else:
        push_url  = f"http://{login}:{token}@{base[7:]}/{cfg.github_org}/{repo_name}.git"
    clean_url = f"{base}/{cfg.github_org}/{repo_name}.git"   # no credentials — for display

    # Check if origin is already set and remote repo is reachable
    existing = subprocess.run(
        ["git", "-C", str(cfg.root), "remote", "get-url", "origin"],
        capture_output=True,
    )
    if existing.returncode == 0:
        # Verify the remote repo actually exists before skipping
        ls = subprocess.run(
            ["git", "ls-remote", "--heads", push_url],
            capture_output=True, timeout=15,
        )
        if ls.returncode == 0:
            out(f"  Remote 'origin' already set and reachable — skipping.")
            return
        # Remote set but repo missing (e.g. after a Gitea reset) — update URL below
        subprocess.run(
            ["git", "-C", str(cfg.root), "remote", "set-url", "origin", push_url],
            check=True, capture_output=True,
        )
    else:
        subprocess.run(
            ["git", "-C", str(cfg.root), "remote", "add", "origin", push_url],
            check=True, capture_output=True,
        )

    # Create repo via API (422 = already exists — treat as OK)
    payload = {"name": repo_name, "private": False, "auto_init": False}
    resp = requests.post(
        cfg.github_repos_api(),
        json=payload,
        headers={"Authorization": f"token {token}",
                 "Accept": "application/vnd.github.v3+json"},
        timeout=15,
    )
    if resp.status_code not in (201, 422):
        raise RuntimeError(
            f"Failed to create repo '{repo_name}': HTTP {resp.status_code} — {resp.text[:200]}"
        )
    if resp.status_code == 201:
        success(f"Created repo {cfg.github_org}/{repo_name}  ({clean_url})")
    else:
        out(f"  Repo {cfg.github_org}/{repo_name} already exists — continuing.")



def _push_jenkins_shared_lib(cfg: PlatformConfig, lib_src: Path):
    """
    Create jenkins-shared-lib repo in Gitea/GitHub and push the local
    jenkins-shared-lib/ directory to it.

    lib_src: path to the jenkins-shared-lib/ source directory (inside the
             platform template tree, NOT inside the deployed platform instance).
    """
    import subprocess, tempfile, shutil, requests

    step("Pushing jenkins-shared-lib to git hosting")

    if not lib_src.is_dir():
        warn(f"jenkins-shared-lib/ not found at {lib_src} — skipping shared lib push.")
        return

    # Create repo (422 = already exists)
    payload = {"name": "jenkins-shared-lib", "private": False, "auto_init": False}
    resp = requests.post(
        cfg.github_repos_api(),
        json=payload,
        headers={"Authorization": f"token {cfg.github_token}",
                 "Accept": "application/vnd.github.v3+json"},
        timeout=15,
    )
    if resp.status_code not in (201, 422):
        raise RuntimeError(
            f"Failed to create jenkins-shared-lib repo: HTTP {resp.status_code} — {resp.text[:200]}"
        )
    if resp.status_code == 201:
        success("Created repo jenkins-shared-lib")
    else:
        out("  Repo jenkins-shared-lib already exists — will push/update.")

    # Build push URL with embedded credentials
    # _GITHUB_LOGIN is set by _validate_tokens(); fall back to github_org admin if empty
    login = _GITHUB_LOGIN or cfg.github_org
    token = cfg.github_token
    # Strip scheme from github_url to embed credentials
    base = cfg.github_url
    if base.startswith("https://"):
        push_url = f"https://{login}:{token}@{base[8:]}/{cfg.github_org}/jenkins-shared-lib.git"
    else:  # http://
        push_url = f"http://{login}:{token}@{base[7:]}/{cfg.github_org}/jenkins-shared-lib.git"

    with tempfile.TemporaryDirectory() as tmpdir:
        # Copy library contents into temp dir
        for item in lib_src.iterdir():
            dest = Path(tmpdir) / item.name
            if item.is_dir():
                shutil.copytree(str(item), str(dest))
            else:
                shutil.copy2(str(item), str(dest))

        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        subprocess.run(["git", "-C", tmpdir, "init", "-b", "main"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "config", "user.email", "platform-bootstrap@ap3.local"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "config", "user.name", "AP3 Bootstrap"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "add", "--all"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "commit", "-m", "chore: initial jenkins-shared-lib push"],
                       check=True, capture_output=True)
        push_result = subprocess.run(
            ["git", "-C", tmpdir, "push", push_url, "main", "--force"],
            capture_output=True, env=env,
        )
        if push_result.returncode != 0:
            err = push_result.stderr.decode()
            raise RuntimeError(
                f"Failed to push jenkins-shared-lib:\n{err}\n\n"
                f"Manual push:\n"
                f"  cd /tmp/jenkins-shared-lib && git push {cfg.github_url}/{cfg.github_org}/jenkins-shared-lib.git main"
            )

    success("jenkins-shared-lib pushed to git hosting")


def _push_extra_libraries(cfg: PlatformConfig, lib_extras_dir: Path):
    """Push any additional libraries found in bootstrap/lib-extras/ as separate repos."""
    if not lib_extras_dir.is_dir():
        return
    import subprocess, tempfile, shutil, requests

    for lib_dir in sorted(lib_extras_dir.iterdir()):
        if not lib_dir.is_dir():
            continue
        repo_name = lib_dir.name
        step(f"Pushing library '{repo_name}' to git hosting")

        payload = {"name": repo_name, "private": False, "auto_init": False}
        resp = requests.post(cfg.github_repos_api(), json=payload,
                             headers={"Authorization": f"token {cfg.github_token}",
                                      "Accept": "application/vnd.github.v3+json"},
                             timeout=15)
        if resp.status_code not in (201, 422):
            warn(f"Could not create repo '{repo_name}': HTTP {resp.status_code} — skipping.")
            continue
        if resp.status_code == 201:
            success(f"Created repo {repo_name}")
        else:
            out(f"  Repo {repo_name} already exists — will push/update.")

        login = _GITHUB_LOGIN or cfg.github_org
        token = cfg.github_token
        base = cfg.github_url
        if base.startswith("https://"):
            push_url = f"https://{login}:{token}@{base[8:]}/{cfg.github_org}/{repo_name}.git"
        else:
            push_url = f"http://{login}:{token}@{base[7:]}/{cfg.github_org}/{repo_name}.git"

        with tempfile.TemporaryDirectory() as tmpdir:
            for item in lib_dir.iterdir():
                dest = Path(tmpdir) / item.name
                if item.is_dir():
                    shutil.copytree(str(item), str(dest))
                else:
                    shutil.copy2(str(item), str(dest))
            env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
            subprocess.run(["git", "-C", tmpdir, "init", "-b", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.email", "platform-bootstrap@ap3.local"],
                           check=True, capture_output=True, env=env)
            subprocess.run(["git", "-C", tmpdir, "config", "user.name", "AP3 Bootstrap"],
                           check=True, capture_output=True, env=env)
            subprocess.run(["git", "-C", tmpdir, "add", "--all"], check=True, capture_output=True)
            subprocess.run(["git", "-C", tmpdir, "commit", "-m", f"chore: initial {repo_name} push"],
                           check=True, capture_output=True)
            result = subprocess.run(["git", "-C", tmpdir, "push", push_url, "main", "--force"],
                                    capture_output=True, env=env)
            if result.returncode != 0:
                warn(f"Failed to push {repo_name}: {result.stderr.decode()[:200]}")
                continue

        # Track in platform.yaml libraries map
        lib_url = f"{cfg.github_url.rstrip('/')}/{cfg.github_org}/{repo_name}.git"
        _register_library(cfg, repo_name, lib_url)
        success(f"Library '{repo_name}' pushed")


def _register_library(cfg: PlatformConfig, name: str, repo_url: str):
    """Add an entry to the libraries: map in platform.yaml."""
    import yaml as _yaml
    cfg_file = cfg.root / "platform.yaml"
    if not cfg_file.exists():
        return
    with open(cfg_file) as f:
        data = _yaml.safe_load(f) or {}
    libs = data.setdefault("libraries", {})
    libs[name] = {
        "repo_url": repo_url,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(cfg_file, "w") as f:
        _yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)


def _configure_jenkins_shared_lib(cfg: PlatformConfig):
    """
    Configure the Jenkins global shared library 'platform-shared-lib' via
    the Jenkins script console (portable — works without a specific plugin CLI).
    """
    import requests

    step("Configuring Jenkins global shared library 'platform-shared-lib'")

    jenkins_url  = cfg.jenkins_url.rstrip("/")
    jenkins_user = cfg.jenkins_user
    jenkins_tok  = cfg.jenkins_token
    auth         = (jenkins_user, jenkins_tok)

    # ── Fetch crumb ───────────────────────────────────────────────────────────
    crumb_resp = requests.get(
        f"{jenkins_url}/crumbIssuer/api/json",
        auth=auth, timeout=10,
    )
    if crumb_resp.status_code == 404:
        # CSRF protection disabled — no crumb needed
        crumb_header = {}
    elif crumb_resp.status_code == 200:
        c = crumb_resp.json()
        crumb_header = {c["crumbRequestField"]: c["crumb"]}
    else:
        raise RuntimeError(
            f"Failed to fetch Jenkins crumb: HTTP {crumb_resp.status_code}"
        )

    lib_repo    = cfg.resolved_shared_lib_url
    default_ver = "main"

    groovy = f"""
import jenkins.model.Jenkins
import org.jenkinsci.plugins.workflow.libs.*
import jenkins.plugins.git.GitSCMSource

def globalLibraries = Jenkins.get().getDescriptor(GlobalLibraries.class)
def existing = globalLibraries.libraries.findAll {{ it.name != 'platform-shared-lib' }}
def scmSource = new GitSCMSource('{lib_repo}')
scmSource.credentialsId = 'github-token'
def lib = new LibraryConfiguration('platform-shared-lib', new SCMSourceRetriever(scmSource))
lib.defaultVersion = '{default_ver}'
lib.implicit = false
lib.allowVersionOverride = true
globalLibraries.libraries = existing + [lib]
Jenkins.get().save()
println "DONE: platform-shared-lib configured"
""".strip()

    resp = requests.post(
        f"{jenkins_url}/script",
        auth=auth,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 **crumb_header},
        data={"script": groovy},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Jenkins script console returned HTTP {resp.status_code}: {resp.text[:300]}"
        )
    if "DONE:" not in resp.text:
        raise RuntimeError(
            f"Groovy script executed but did not print expected marker.\n"
            f"Response (first 400 chars):\n{resp.text[:400]}"
        )
    success("Jenkins global shared library 'platform-shared-lib' configured")


# ── Entry point ───────────────────────────────────────────────────────────────

def run(demo: bool = False, yes_mode: bool = False, config_path: str = "",
        platform_src_dir: str = "", platform_target_dir: str = ""):
    """
    platform_src_dir:    path to the platform/ template inside the bootstrap toolkit
                         (set by bootstrap.sh before calling wizard.py)
    platform_target_dir: where the new platform instance should be created
                         (default: ../platform relative to bootstrap toolkit)
    """
    import shutil as _shutil

    global _YES_MODE, _CONFIG, _CONFIG_PATH
    _YES_MODE = yes_mode

    if config_path:
        _CONFIG      = _load_config(config_path)
        _CONFIG_PATH = config_path
        _YES_MODE    = True   # config mode is always non-interactive

    # Resolve platform source and target directories
    bootstrap_dir = Path(__file__).parent.parent   # bootstrap/
    toolkit_root  = bootstrap_dir.parent           # repo root

    src_dir = Path(platform_src_dir) if platform_src_dir else toolkit_root / "platform"

    raw_target = platform_target_dir or (_CONFIG or {}).get("platform_target_dir", "../platform")
    target_dir = Path(raw_target)
    if not target_dir.is_absolute():
        target_dir = (toolkit_root / target_dir).resolve()

    # ── Copy platform template to target directory ────────────────────────────
    if target_dir.exists() and (target_dir / ".git").exists():
        warn(f"Platform instance already exists at {target_dir} — skipping copy.")
    else:
        step(f"Copying platform template to {target_dir}")
        if target_dir.exists():
            _shutil.rmtree(target_dir)
        _shutil.copytree(str(src_dir), str(target_dir),
                         ignore=_shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
        import subprocess as _sp
        _sp.run(["git", "-C", str(target_dir), "init", "-b", "main"],
                check=True, capture_output=True)
        success(f"Platform template copied to {target_dir}")

    # ── Bootstrap config is now in the target dir ─────────────────────────────
    target_config = target_dir / "platform.yaml"
    cfg = PlatformConfig(config_path=str(target_config))

    if not _YES_MODE:
        _welcome()

    _collect_integrations(cfg)

    if _CONFIG:
        _validate_tokens(cfg)

    platform, cluster_prefix = _collect_platform_defaults(cfg)

    env_defs = _collect_environments(platform, cluster_prefix, cfg)
    env_defs += _collect_extra_environments(platform, cluster_prefix, cfg)

    if not env_defs:
        print("\n  No environments to create. Exiting.")
        sys.exit(0)

    if not _YES_MODE and not _confirm_plan(env_defs, demo):
        print("\n  Aborted.")
        sys.exit(0)

    hr()
    print()
    step("Creating environments")
    created_names = [d.name for d in env_defs]
    _remove_stub_envs(cfg, created_names)
    _create_environments(cfg, env_defs)
    # Note: cluster profiles already written during _collect_* steps.
    # We only need to update the default_cluster_* keys.
    _update_platform_yaml_defaults(cfg, env_defs)

    if demo:
        _seed_demo_data(cfg, env_defs)

    shared_lib_repo_name = (_CONFIG or {}).get("shared_lib_repo_name", "jenkins-shared-lib")
    lib_src = src_dir / "jenkins-shared-lib"

    if _CONFIG:
        hr()
        print()
        repo_name = _CONFIG.get("platform_repo_name", "platform")
        _create_platform_repo(cfg, repo_name)
        _push_jenkins_shared_lib(cfg, lib_src)
        # Push extra libraries from bootstrap/lib-extras/
        _push_extra_libraries(cfg, bootstrap_dir / "lib-extras")
        _configure_jenkins_shared_lib(cfg)

    # ── Write bootstrap state file for delete.sh ─────────────────────────────
    import yaml as _yaml
    state = {
        "platform_target_dir": str(target_dir),
        "platform_repo_name":  (_CONFIG or {}).get("platform_repo_name", "platform"),
        "shared_lib_repo_name": shared_lib_repo_name,
        "github_url":   cfg.github_url,
        "github_org":   cfg.github_org,
        "jenkins_url":  cfg.jenkins_url,
        "sonarqube_url": cfg.sonarqube_url,
        "bootstrapped_at": datetime.now(timezone.utc).isoformat(),
    }
    state_file = bootstrap_dir / ".bootstrap-state.yaml"
    with open(state_file, "w") as f:
        _yaml.safe_dump(state, f, default_flow_style=False)

    _print_summary(env_defs, demo)


def main():
    parser = argparse.ArgumentParser(
        description="AP3 Platform bootstrap wizard — create your initial environments interactively.",
    )
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip all confirmation prompts (CI / non-interactive mode). "
                             "Uses defaults for everything not explicitly overridden.")
    parser.add_argument("--config", "-c", metavar="FILE",
                        help="Config file with pre-filled wizard answers (non-interactive). "
                             "Mutually exclusive with --yes.")
    parser.add_argument("--demo", action="store_true",
                        help="Seed demo service data after environment creation.")
    parser.add_argument("--platform-src", metavar="DIR",
                        help="Path to the platform/ template directory (set by bootstrap.sh).")
    parser.add_argument("--platform-target", metavar="DIR",
                        help="Where to create the platform instance (default: ../platform).")
    args = parser.parse_args()

    if args.config and args.yes:
        parser.error("--config and --yes are mutually exclusive.")

    run(demo=args.demo, yes_mode=args.yes, config_path=args.config or "",
        platform_src_dir=args.platform_src or "",
        platform_target_dir=args.platform_target or "")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
platform.py — Platform CLI
Single entry point for all platform operations.

Usage:
  python platform.py service create --name my-svc --template springboot --owner team-x
  python platform.py service list
  python platform.py service info --name my-svc
  python platform.py env list
  python platform.py env create --name my-poc --type poc --base staging
  python platform.py env destroy --name poc-my-poc-20260403
  python platform.py deploy --env dev --service my-svc --version 1.2.0
  python platform.py release-notes --service my-svc
"""

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from service_creator import ServiceCreator
from env_manager import EnvManager
from deployer import Deployer
from release_notes import ReleaseNotesGenerator
from history import HistoryCollector, format_history_table
from cluster_manager import ClusterManager
from template_manager import TemplateManager
from config import PlatformConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="platform",
        description="Platform CLI — manage services, environments and deployments",
    )
    parser.add_argument(
        "--config", default=None, help="Path to platform config file (default: auto-detect)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print actions without executing them"
    )
    parser.add_argument(
        "--json", action="store_true", help="Output results as JSON"
    )

    sub = parser.add_subparsers(dest="resource", required=True)

    # ── SERVICE ──────────────────────────────────────────────────────────────
    svc = sub.add_parser("service", help="Service management")
    svc_sub = svc.add_subparsers(dest="action", required=True)

    # service create
    sc = svc_sub.add_parser("create", help="Bootstrap a new AP3 service")
    sc.add_argument("--name",     required=True, help="Service name (kebab-case)")
    sc.add_argument("--owner",    required=True, help="Owning team")
    sc.add_argument("--description", default="", help="Short service description")

    # Source mode — mutually exclusive
    mode_group = sc.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--template",
        default=None,
        help="[AP3-hosted] Create a new GitHub repo scaffolded from this template "
             "(default: springboot). Run 'platform template list' to see available templates.",
    )
    mode_group.add_argument(
        "--fork-from",
        default=None,
        metavar="SERVICE",
        help="[AP3-hosted] Create a new GitHub repo by forking an existing AP3 service. "
             "The fork starts as a full copy of the source service.",
    )
    mode_group.add_argument(
        "--external-repo",
        default=None,
        metavar="URL",
        help="[External] Reference an existing GitHub/Git repo by URL. "
             "AP3 will NOT create or scaffold a repo — it only registers the "
             "service in Jenkins and the dev environment.",
    )

    sc.add_argument("--no-jenkins",  action="store_true",
                    help="Skip Jenkins pipeline registration")
    sc.add_argument("--force", action="store_true",
                    help="Skip confirmation prompt")

    # service remove
    sr = svc_sub.add_parser(
        "remove",
        help="Remove a service from all environments, destroy its Jenkins pipeline, "
             "and deregister it from GitHub (repo is kept)",
    )
    sr.add_argument("--name",  required=True, help="Service name")
    sr.add_argument("--force", action="store_true",
                    help="Skip confirmation prompt")

    # service jenkins-register
    sjr = svc_sub.add_parser("jenkins-register",
                              help="Create (or recreate) the Jenkins multibranch pipeline for a service")
    sjr.add_argument("name", help="Service name")

    # service list
    svc_sub.add_parser("list", help="List all known services")

    # service info
    si = svc_sub.add_parser("info", help="Show service details")
    si.add_argument("--name", required=True, help="Service name")

    # ── ENV ──────────────────────────────────────────────────────────────────
    env = sub.add_parser("env", help="Environment management")
    env_sub = env.add_subparsers(dest="action", required=True)

    # env list
    env_sub.add_parser("list", help="List all environments")

    # env info
    ei = env_sub.add_parser("info", help="Show environment details")
    ei.add_argument("--name", required=True, help="Environment name")

    # env create
    ec = env_sub.add_parser("create", help="Create a new environment (POC or fixed)")
    ec.add_argument("--name",  required=True, help="Short name (will be prefixed with poc-)")
    ec.add_argument("--type",  required=True, choices=["poc", "fixed"], default="poc")
    ec.add_argument("--base",  default="staging",
                    help="Base environment to fork versions from")
    ec.add_argument("--platform", choices=["openshift", "aws"], default=None,
                    help="Target platform: 'openshift' or 'aws'. "
                         "Derived from the cluster profile when omitted.")
    ec.add_argument("--cluster", default=None,
                    help="Target cluster name as defined in platform.yaml. "
                         "Defaults to the base environment's cluster.")
    ec.add_argument("--namespace", default=None,
                    help="Pre-existing namespace to use. "
                         "Required when you have no rights to create namespaces. "
                         "Defaults to 'platform-{full-env-name}'.")
    ec.add_argument("--owner", default=None, help="Owner (defaults to git user)")
    ec.add_argument("--description", default="", help="POC purpose description")
    ec.add_argument("--ttl-days", type=int, default=14,
                    help="Time-to-live in days for POC environments (default: 14, max: 365)")
    ec.add_argument("--force", action="store_true",
                    help="Skip confirmation prompt (show disclaimer but do not ask)")

    # env destroy
    ed = env_sub.add_parser("destroy", help="Destroy an ephemeral environment")
    ed.add_argument("--name", required=True, help="Full environment name")
    ed.add_argument("--force", action="store_true",
                    help="Skip confirmation prompt")

    # env extend — postpone TTL expiry
    ext = env_sub.add_parser("extend", help="Postpone TTL expiry of a POC environment")
    ext.add_argument("--name",     required=True, help="Full POC environment name")
    ext.add_argument("--ttl-days", type=int, default=14,
                     help="Number of additional days to add (default: 14, max: 365 total)")

    # env diff
    edi = env_sub.add_parser("diff", help="Show version diff between two environments")
    edi.add_argument("--from", dest="env_from", required=True)
    edi.add_argument("--to",   dest="env_to",   required=True)

    # ── DEPLOY ───────────────────────────────────────────────────────────────
    dep = sub.add_parser("deploy", help="Trigger a deployment")
    dep.add_argument("--env",     required=True, help="Target environment")
    dep.add_argument("--service", required=True, help="Service name")
    dep.add_argument("--version", required=True, help="Version to deploy (semver)")
    dep.add_argument("--wait",    action="store_true",
                     help="Wait for rollout to complete")
    dep.add_argument("--platform", choices=["openshift", "aws"], default=None,
                     help="Override target platform for this deploy. "
                          "Normally derived from the environment's cluster profile.")
    dep.add_argument("--force", action="store_true",
                     help="Skip confirmation prompt (show disclaimer but do not ask)")

    # ── RELEASE-NOTES ────────────────────────────────────────────────────────
    rn = sub.add_parser("release-notes", help="Generate or display release notes")
    rn.add_argument("--service", required=True, help="Service name")
    rn.add_argument("--version", default=None,
                    help="Specific version (default: latest)")

    # ── HISTORY ──────────────────────────────────────────────────────────────
    hist = sub.add_parser("history", help="Show platform audit log")
    hist.add_argument("--env",     default=None, help="Filter by environment")
    hist.add_argument("--service", default=None, help="Filter by service")
    hist.add_argument("--actor",   default=None, help="Filter by actor (partial match)")
    hist.add_argument("--type",    default=None, dest="event_type",
                      choices=["env_create", "env_destroy", "env_update",
                               "deploy", "service_reg", "reset"],
                      help="Filter by event type")
    hist.add_argument("--limit",   default=50, type=int,
                      help="Maximum number of events to show (default: 50)")
    hist.add_argument("--full",    action="store_true", default=False,
                      help="Show full history across all resets (default: since last reset)")

    # ── CONFIG ───────────────────────────────────────────────────────────────
    cfg_cmd = sub.add_parser("config",
                             help="Show or update platform.yaml integration settings")
    cfg_sub = cfg_cmd.add_subparsers(dest="action", required=True)

    # config show
    cfg_sub.add_parser("show", help="Display current platform settings")

    # config set
    cs = cfg_sub.add_parser("set", help="Update a platform setting")
    cs.add_argument("--github-url",          default=None,
                    help="GitHub base URL (e.g. https://github.mycompany.com)")
    cs.add_argument("--github-account-type", default=None,
                    choices=["org", "user"],
                    help="Whether repos belong to an org or a personal user account")
    cs.add_argument("--github-org",          default=None,
                    help="GitHub organisation name or username")
    cs.add_argument("--jenkins-url",         default=None,
                    help="Jenkins base URL")

    # ── CLUSTER ──────────────────────────────────────────────────────────────
    cl = sub.add_parser("cluster", help="Manage cluster profiles in platform.yaml")
    cl_sub = cl.add_subparsers(dest="action", required=True)

    # cluster list
    cl_sub.add_parser("list", help="List all cluster profiles")

    # cluster info
    ci = cl_sub.add_parser("info", help="Show cluster profile details")
    ci.add_argument("--name", required=True, help="Cluster name")

    # cluster add — OpenShift
    ca = cl_sub.add_parser("add", help="Add or update a cluster profile")
    ca.add_argument("--name",     required=True, help="Cluster name (e.g. openshift-dev)")
    ca.add_argument("--platform", required=True, choices=["openshift", "aws"],
                    help="Platform type")
    # OpenShift-specific
    ca.add_argument("--api-url",  default="",
                    help="[OpenShift] API server URL (e.g. https://api.cluster.example.com:6443)")
    ca.add_argument("--context",  default="",
                    help="[OpenShift] kubeconfig context name")
    # AWS-specific
    ca.add_argument("--region",       default="",
                    help="[AWS] AWS region (e.g. eu-west-1)")
    ca.add_argument("--cluster-name", default="",
                    help="[AWS] EKS cluster name (used with aws eks update-kubeconfig)")
    # Shared
    ca.add_argument("--registry",            default="",
                    help="Container registry URL (defaults to platform-level registry)")
    ca.add_argument("--helm-values-suffix",  default="",
                    help="Helm values file suffix — resolves to helm/values-{suffix}.yaml "
                         "(defaults to last segment of cluster name)")

    # cluster remove
    cr = cl_sub.add_parser("remove", help="Remove a cluster profile from platform.yaml")
    cr.add_argument("--name",  required=True, help="Cluster name to remove")
    cr.add_argument("--force", action="store_true",
                    help="Remove even if environments still reference this cluster")

    # ── STATUS ───────────────────────────────────────────────────────────────
    sta = sub.add_parser("status", help="Compare expected vs live cluster state")
    sta.add_argument("--env", default=None,
                     help="Check a single environment (default: all)")

    # ── TEMPLATE ─────────────────────────────────────────────────────────────
    tpl = sub.add_parser("template", help="Manage scaffold templates")
    tpl_sub = tpl.add_subparsers(dest="action", required=True)

    # template list
    tpl_sub.add_parser("list", help="List available scaffold templates")

    # template info
    ti = tpl_sub.add_parser("info", help="Show template details")
    ti.add_argument("--name", required=True, help="Template name")

    # template add
    ta = tpl_sub.add_parser("add", help="Add a new scaffold template from a local directory")
    ta.add_argument("--name",        required=True, help="Template name (kebab-case)")
    ta.add_argument("--from-dir",    required=True, metavar="DIR",
                    help="Path to a directory whose contents become the template")
    ta.add_argument("--description", default="", help="Short description")
    ta.add_argument("--language",    default="", help="Primary language (e.g. java, python, javascript)")
    ta.add_argument("--force",       action="store_true",
                    help="Overwrite if a template with this name already exists")

    # template remove
    tr = tpl_sub.add_parser("remove", help="Remove a scaffold template")
    tr.add_argument("--name",  required=True, help="Template name")
    tr.add_argument("--force", action="store_true",
                    help="Remove even if services in the catalog reference this template")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    cfg = PlatformConfig(args.config)

    try:
        if args.resource == "service":
            handler = ServiceCreator(cfg, dry_run=args.dry_run, json_output=args.json)
            if args.action == "create":
                # Resolve source mode from mutually-exclusive flags
                if args.fork_from:
                    source_mode, template, fork_from, external_repo = (
                        "fork", "springboot", args.fork_from, "")
                elif args.external_repo:
                    source_mode, template, fork_from, external_repo = (
                        "external", "springboot", "", args.external_repo)
                else:
                    source_mode, template, fork_from, external_repo = (
                        "template", args.template or "springboot", "", "")
                handler.create(
                    name=args.name,
                    owner=args.owner,
                    description=args.description,
                    source_mode=source_mode,
                    template=template,
                    fork_from=fork_from,
                    external_repo_url=external_repo,
                    skip_jenkins=args.no_jenkins,
                    force=args.force,
                )
            elif args.action == "remove":
                handler.remove(name=args.name, force=args.force)
            elif args.action == "jenkins-register":
                svc_catalog = cfg.load_service(args.name)
                handler._register_jenkins_pipeline(
                    args.name, svc_catalog.get("repo_url", "")
                )
                from output import success
                success(f"Jenkins pipeline registered for '{args.name}'")
            elif args.action == "list":
                handler.list_services()
            elif args.action == "info":
                handler.info(args.name)

        elif args.resource == "env":
            handler = EnvManager(cfg, dry_run=args.dry_run, json_output=args.json)
            if args.action == "list":
                handler.list_envs()
            elif args.action == "info":
                handler.info(args.name)
            elif args.action == "create":
                handler.create(
                    name=args.name,
                    env_type=args.type,
                    base=args.base,
                    namespace=args.namespace,
                    cluster=args.cluster,
                    platform=args.platform,
                    owner=args.owner,
                    description=args.description,
                    ttl_days=args.ttl_days,
                    force=args.force,
                )
            elif args.action == "destroy":
                handler.destroy(args.name, force=args.force)
            elif args.action == "extend":
                handler.extend(args.name, ttl_days=args.ttl_days)
            elif args.action == "diff":
                handler.diff(args.env_from, args.env_to)

        elif args.resource == "deploy":
            handler = Deployer(cfg, dry_run=args.dry_run, json_output=args.json)
            handler.deploy(
                env=args.env,
                service=args.service,
                version=args.version,
                wait=args.wait,
                force=args.force,
            )

        elif args.resource == "release-notes":
            handler = ReleaseNotesGenerator(cfg, json_output=args.json)
            handler.show(service=args.service, version=args.version)

        elif args.resource == "config":
            import yaml as _yaml
            platform_file = cfg.root / "platform.yaml"
            if args.action == "show":
                print()
                print(f"  github_url          : {cfg.github_url}")
                print(f"  github_account_type : {cfg.github_account_type}")
                print(f"  github_org          : {cfg.github_org}")
                print(f"  jenkins_url         : {cfg.jenkins_url}")
                print()
                print(f"  GITHUB_TOKEN  : {'SET' if cfg.github_token else 'NOT SET'}")
                print(f"  JENKINS_USER  : {'SET' if cfg.jenkins_user else 'NOT SET'}")
                print(f"  JENKINS_TOKEN : {'SET' if cfg.jenkins_token else 'NOT SET'}")
                print()
            elif args.action == "set":
                with open(platform_file) as f:
                    data = _yaml.safe_load(f) or {}
                changed = []
                for attr, key in [
                    (args.github_url,          "github_url"),
                    (args.github_account_type, "github_account_type"),
                    (args.github_org,          "github_org"),
                    (args.jenkins_url,         "jenkins_url"),
                ]:
                    if attr is not None:
                        data[key] = attr
                        changed.append(f"{key}={attr}")
                if not changed:
                    print("  Nothing to update — provide at least one --flag.")
                else:
                    with open(platform_file, "w") as f:
                        _yaml.dump(data, f, default_flow_style=False,
                                   allow_unicode=True, sort_keys=False)
                    print(f"  Updated: {', '.join(changed)}")

        elif args.resource == "cluster":
            mgr = ClusterManager(cfg, json_output=args.json)
            if args.action == "list":
                mgr.list_clusters()
            elif args.action == "info":
                mgr.info(args.name)
            elif args.action == "add":
                mgr.add(
                    name=args.name,
                    platform=args.platform,
                    api_url=args.api_url,
                    context=args.context,
                    region=args.region,
                    cluster_name=args.cluster_name,
                    registry=args.registry,
                    helm_values_suffix=args.helm_values_suffix,
                )
            elif args.action == "remove":
                mgr.remove(args.name, force=args.force)

        elif args.resource == "template":
            mgr = TemplateManager(cfg, dry_run=args.dry_run, json_output=args.json)
            if args.action == "list":
                mgr.list_templates()
            elif args.action == "info":
                mgr.info(args.name)
            elif args.action == "add":
                mgr.add(
                    name=args.name,
                    from_dir=args.from_dir,
                    description=args.description,
                    language=args.language,
                    force=args.force,
                )
            elif args.action == "remove":
                mgr.remove(args.name, force=args.force)

        elif args.resource == "status":
            from status_checker import StatusChecker, format_status_table
            checker = StatusChecker(cfg)
            results = [checker.check_env(args.env)] if args.env else checker.check_all()
            if args.json:
                import json as _json
                print(_json.dumps([r.as_dict() for r in results], indent=2))
            else:
                print(format_status_table(results))

        elif args.resource == "history":
            collector = HistoryCollector(cfg)
            events = collector.collect(
                env_filter=args.env,
                service_filter=args.service,
                actor_filter=args.actor,
                event_type_filter=args.event_type,
                limit=args.limit,
                full=args.full,
            )
            if args.json:
                import json as _json
                print(_json.dumps([e.as_dict() for e in events], indent=2))
            else:
                print(format_history_table(events))

    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)
    except SystemExit:
        # error_exit() already printed the message; re-raise so the process exits non-zero.
        # In JSON mode also emit a machine-readable stub so _run_cli can detect the failure.
        if "--json" in sys.argv:
            import json as _json
            # stderr already has the human message; stdout gets the JSON envelope
            print(_json.dumps({"error": "command failed — see stderr for details"}))
        raise
    except Exception as e:
        msg = str(e)
        if os.environ.get("PLATFORM_DEBUG"):
            import traceback
            traceback.print_exc()
        if "--json" in sys.argv:
            import json as _json
            print(_json.dumps({"error": msg}))
        else:
            print(f"\n[error] {msg}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

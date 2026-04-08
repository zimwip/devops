#!/usr/bin/env python3
"""
drift_checker.py — Automated drift detection and POC expiry management.

Runs every 15 minutes via Jenkins (driftChecker.groovy).

What it does:
  1. Iterates all environments
  2. Compares desired state (envs/{env}/{service}/version.yaml)
     against actual running state on the cluster (via status_checker.py)
  3. On drift → sends Slack notification, auto-remediates on 'dev' only
  4. Checks POC TTL expiry:
     - 6h before expiry: Slack warning to contact_slack channel
     - At/after expiry: triggers automatic teardown

Environment variables:
  SLACK_WEBHOOK_URL   — Slack incoming webhook URL for drift notifications
  SLACK_CHANNEL       — Default Slack channel (e.g. #platform-alerts)
  PLATFORM_CONFIG_DIR — Path to platform-config repo root (default: auto-detect)
  AUTO_REMEDIATE_ENVS — Comma-separated list of envs to auto-remediate (default: dev)
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml


def _find_platform_root() -> Path:
    if config_dir := os.environ.get("PLATFORM_CONFIG_DIR"):
        return Path(config_dir)
    p = Path.cwd()
    for _ in range(8):
        if (p / "envs").is_dir() and (p / "scripts").is_dir():
            return p
        p = p.parent
    return Path(__file__).parent.parent


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Slack ─────────────────────────────────────────────────────────────────────

def notify_slack(channel: str, text: str, webhook_url: str = ""):
    """Send a Slack notification. Silently skips if no webhook configured."""
    url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL", "")
    if not url:
        print(f"  [slack-skip] {channel}: {text[:120]}")
        return
    try:
        payload = {"channel": channel, "text": text, "username": "Platform Bot"}
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"  [slack-warn] Webhook returned {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"  [slack-warn] Failed to send notification: {e}")


# ── Drift detection ────────────────────────────────────────────────────────────

def check_all_envs_for_drift(cfg, default_channel: str, auto_remediate_envs: list[str]):
    """Check all environments for drift. Auto-remediate on configured envs."""
    from status_checker import StatusChecker, STATUS_DRIFT, STATUS_MISSING

    checker = StatusChecker(cfg)
    any_drift = False

    for env_name in cfg.list_envs():
        print(f"\n  Checking {env_name}...")
        try:
            env_status = checker.check_env(env_name)
        except Exception as e:
            print(f"  [error] Could not check {env_name}: {e}")
            continue

        if not env_status.reachable:
            print(f"  [unreachable] {env_name}: {env_status.error or 'cluster unavailable'}")
            continue

        drifted_services = [
            s for s in env_status.services
            if s.status in (STATUS_DRIFT, STATUS_MISSING)
        ]

        if not drifted_services:
            print(f"  [ok] {env_name}: all {len(env_status.services)} service(s) healthy")
            continue

        any_drift = True
        drift_lines = []
        for svc in drifted_services:
            line = (
                f"  *{svc.name}*: expected `{svc.expected_version}` "
                f"but running `{svc.running_version or 'nothing'}` "
                f"({svc.status})"
            )
            drift_lines.append(line)
            print(f"  [drift] {env_name}/{svc.name}: {svc.status} "
                  f"(expected {svc.expected_version}, running {svc.running_version})")

        # Slack notification
        msg = (
            f":warning: *Drift detected in `{env_name}`* "
            f"({env_status.cluster} / {env_status.namespace})\n"
            + "\n".join(drift_lines)
        )
        notify_slack(default_channel, msg)

        # Auto-remediation on configured envs
        if env_name in auto_remediate_envs:
            print(f"  [auto-remediate] Triggering re-deploy for drifted services in {env_name}")
            for svc in drifted_services:
                _remediate(cfg, env_name, svc.name, svc.expected_version, default_channel)

    return any_drift


def _remediate(cfg, env_name: str, service: str, version: str, channel: str):
    """Trigger a Helm re-deploy to restore the desired version."""
    try:
        from deployer import Deployer
        deployer = Deployer(cfg, dry_run=False, json_output=False)
        deployer.deploy(env=env_name, service=service, version=version, force=True)
        notify_slack(
            channel,
            f":white_check_mark: Auto-remediated `{service}:{version}` in `{env_name}`"
        )
        print(f"  [remediated] {env_name}/{service} → {version}")
    except Exception as e:
        err_msg = f":x: Auto-remediation FAILED for `{service}` in `{env_name}`: {e}"
        notify_slack(channel, err_msg)
        print(f"  [remediate-error] {env_name}/{service}: {e}")


# ── POC expiry ────────────────────────────────────────────────────────────────

def check_poc_expiry(cfg, default_channel: str):
    """Check all POC environments for TTL expiry.

    - 6h before expiry: Slack warning to contact_slack (falls back to default_channel)
    - At/after expiry: automatic teardown
    """
    for env_name in cfg.list_envs():
        try:
            manifest = cfg.load_env_manifest(env_name)
        except Exception:
            continue

        if manifest.get("type") != "poc":
            continue

        expires_str = manifest.get("expires_at", "")
        if not expires_str:
            continue

        try:
            expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
        except ValueError:
            print(f"  [poc-warn] Could not parse expires_at for {env_name}: {expires_str}")
            continue

        now = _now()
        remaining = expires_at - now
        remaining_hours = remaining.total_seconds() / 3600
        contact = manifest.get("contact_slack") or default_channel

        if remaining.total_seconds() <= 0:
            # Expired — automatic teardown
            overdue_h = abs(remaining_hours)
            print(f"  [poc-expired] {env_name}: expired {overdue_h:.1f}h ago — triggering teardown")
            notify_slack(
                contact,
                f":skull: POC environment `{env_name}` has expired "
                f"({overdue_h:.0f}h ago). Triggering automatic teardown."
            )
            _teardown_poc(cfg, env_name, default_channel)

        elif remaining_hours <= 6:
            # Warn: 6h warning
            print(f"  [poc-warning] {env_name}: expires in {remaining_hours:.1f}h")
            notify_slack(
                contact,
                f":warning: POC environment `{env_name}` expires in "
                f"*{remaining_hours:.0f} hours* ({expires_at.strftime('%Y-%m-%d %H:%M UTC')}).\n"
                f"Extend: `platform_cli.py env extend --name {env_name} --ttl-days 7`\n"
                f"Destroy: `platform_cli.py env destroy --name {env_name}`"
            )
        else:
            print(f"  [poc-ok] {env_name}: expires in {remaining_hours:.0f}h")


def _teardown_poc(cfg, env_name: str, channel: str):
    """Destroy a POC environment and notify on completion."""
    try:
        from env_manager import EnvManager
        mgr = EnvManager(cfg, dry_run=False, json_output=False)
        mgr.destroy(name=env_name, force=True)
        notify_slack(
            channel,
            f":wastebasket: POC environment `{env_name}` has been automatically torn down."
        )
        print(f"  [poc-torn-down] {env_name}")
    except Exception as e:
        err_msg = f":x: Automatic teardown of `{env_name}` FAILED: {e}"
        notify_slack(channel, err_msg)
        print(f"  [poc-teardown-error] {env_name}: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Drift detection and POC expiry checker")
    parser.add_argument("--dry-run", action="store_true",
                        help="Check only, no auto-remediation or teardown")
    parser.add_argument("--env", default="",
                        help="Check only this environment")
    parser.add_argument("--skip-drift", action="store_true",
                        help="Skip drift detection, only check POC expiry")
    parser.add_argument("--skip-poc-expiry", action="store_true",
                        help="Skip POC expiry check")
    args = parser.parse_args()

    # Bootstrap
    root = _find_platform_root()
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))

    from config import PlatformConfig
    cfg = PlatformConfig(config_path=str(root / "platform.yaml"))

    default_channel = os.environ.get("SLACK_CHANNEL", "#platform-alerts")
    auto_remediate_raw = os.environ.get("AUTO_REMEDIATE_ENVS", "dev")
    auto_remediate_envs: list[str] = (
        []
        if args.dry_run
        else [e.strip() for e in auto_remediate_raw.split(",") if e.strip()]
    )

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Drift + Expiry check — "
          f"{_now().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Auto-remediate envs: {auto_remediate_envs or 'none'}")
    print(f"  Default Slack channel: {default_channel}")

    exit_code = 0

    # 1. Drift detection
    if not args.skip_drift:
        print("\n[1/2] Checking for drift...")
        try:
            has_drift = check_all_envs_for_drift(cfg, default_channel, auto_remediate_envs)
            if has_drift:
                exit_code = 1  # Non-zero so Jenkins marks the run as unstable
        except Exception as e:
            print(f"  [error] Drift check failed: {e}")
            exit_code = 2

    # 2. POC expiry
    if not args.skip_poc_expiry:
        print("\n[2/2] Checking POC expiry...")
        try:
            check_poc_expiry(cfg, default_channel)
        except Exception as e:
            print(f"  [error] POC expiry check failed: {e}")
            exit_code = max(exit_code, 2)

    print(f"\nDrift check complete — exit code {exit_code}")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

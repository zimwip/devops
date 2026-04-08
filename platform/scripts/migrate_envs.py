#!/usr/bin/env python3
"""
migrate_envs.py — One-shot migration from flat versions.yaml to per-service layout.

Converts:
  envs/{env}/versions.yaml           (old: all services in one file)

To:
  envs/{env}/envs.yaml               (new: environment manifest)
  envs/{env}/{service}/version.yaml  (new: per-service version state)
  envs/{env}/{service}/values.yaml   (new: Helm values override, empty placeholder)

The original versions.yaml is renamed to versions.yaml.bak after migration.
Re-running on already-migrated environments is safe (skipped if envs.yaml exists).

Usage:
    python migrate_envs.py [--dry-run] [--env <name>]

Options:
    --dry-run   Show what would be done without writing anything
    --env NAME  Migrate only the named environment (default: all)
"""

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


def _find_platform_root() -> Path:
    p = Path.cwd()
    for _ in range(8):
        if (p / "envs").is_dir() and (p / "scripts").is_dir():
            return p
        p = p.parent
    return Path(__file__).parent.parent


def migrate_env(env_dir: Path, dry_run: bool) -> bool:
    """Migrate a single environment directory. Returns True if migrated."""
    env_name = env_dir.name
    versions_file = env_dir / "versions.yaml"
    envs_yaml = env_dir / "envs.yaml"

    if not versions_file.exists():
        print(f"  [{env_name}] No versions.yaml found — skipping")
        return False

    if envs_yaml.exists():
        print(f"  [{env_name}] Already migrated (envs.yaml exists) — skipping")
        return False

    # Load the old flat structure
    with open(versions_file) as fh:
        data = yaml.safe_load(fh) or {}

    meta     = data.get("_meta", {})
    services = data.get("services") or {}

    env_type_raw = meta.get("env_type", "standard")
    env_type = "poc" if env_type_raw == "poc" else "standard"

    # ── Build envs.yaml ───────────────────────────────────────────────────────
    manifest: dict = {
        "name":              env_name,
        "type":              env_type,
        "cluster":           meta.get("cluster", "openshift-dev"),
        "namespace_pattern": f"{env_name}-{{service}}",
        "platform":          meta.get("platform", "openshift"),
        "registry":          meta.get("registry", "registry.internal"),
        "created_at":        meta.get("updated_at", datetime.now(timezone.utc).isoformat()),
        "updated_at":        meta.get("updated_at", datetime.now(timezone.utc).isoformat()),
        "updated_by":        meta.get("updated_by", "migrate_envs.py"),
    }
    if env_type == "poc":
        manifest.update({
            "base_env":          meta.get("base_env", ""),
            "owner":             meta.get("owner", ""),
            "description":       meta.get("description", ""),
            "expires_at":        meta.get("expires_at", ""),
            "contact_slack":     meta.get("contact_slack", ""),
            "branch_convention": meta.get("branch_convention", ""),
            "services_modified": meta.get("services_modified", []),
            "services_stable":   meta.get("services_stable", []),
        })

    # ── Build per-service version.yaml files ──────────────────────────────────
    service_files: dict[str, dict] = {}
    for svc, svc_data in services.items():
        version = svc_data.get("version", "")
        image   = svc_data.get("image", "")
        # Derive chart_repo from image registry
        registry = image.rsplit("/", 2)[0] if "/" in image else "registry.internal"
        service_files[svc] = {
            "service":      svc,
            "chart_version": version,
            "image_tag":    version,
            "chart_repo":   f"oci://{registry}/helm-local",
            "image":        image,
            "deployed_at":  svc_data.get("deployed_at", ""),
            "deployed_by":  svc_data.get("deployed_by", ""),
            "health":       svc_data.get("health", ""),
        }

    # ── Report ─────────────────────────────────────────────────────────────────
    print(f"  [{env_name}] type={env_type}, {len(service_files)} service(s)")
    print(f"    → {env_dir.relative_to(env_dir.parent.parent)}/envs.yaml")
    for svc in sorted(service_files):
        print(f"    → {env_dir.relative_to(env_dir.parent.parent)}/{svc}/version.yaml")

    if dry_run:
        return False

    # ── Write new files ─────────────────────────────────────────────────────────
    with open(envs_yaml, "w") as fh:
        yaml.dump(manifest, fh, default_flow_style=False, allow_unicode=True, sort_keys=False)

    for svc, svc_data in service_files.items():
        svc_dir = env_dir / svc
        svc_dir.mkdir(exist_ok=True)
        with open(svc_dir / "version.yaml", "w") as fh:
            yaml.dump(svc_data, fh, default_flow_style=False, allow_unicode=True, sort_keys=False)
        # Create an empty values.yaml placeholder for Helm overrides
        values_file = svc_dir / "values.yaml"
        if not values_file.exists():
            values_file.write_text("# Helm values override for this service in this environment.\n"
                                   "# Add key: value pairs to override chart defaults.\n")

    # Rename the old versions.yaml to .bak (preserve for rollback)
    bak = env_dir / "versions.yaml.bak"
    shutil.move(str(versions_file), str(bak))
    print(f"    (versions.yaml renamed to versions.yaml.bak)")
    return True


def main():
    parser = argparse.ArgumentParser(description="Migrate envs/ from flat to per-service layout")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without writing anything")
    parser.add_argument("--env", default="",
                        help="Migrate only this environment (default: all)")
    args = parser.parse_args()

    root = _find_platform_root()
    envs_dir = root / "envs"

    if not envs_dir.exists():
        print(f"ERROR: envs/ directory not found at {envs_dir}", file=sys.stderr)
        sys.exit(1)

    if args.env:
        env_dirs = [envs_dir / args.env]
        if not env_dirs[0].is_dir():
            print(f"ERROR: Environment '{args.env}' not found at {env_dirs[0]}", file=sys.stderr)
            sys.exit(1)
    else:
        env_dirs = sorted(d for d in envs_dir.iterdir() if d.is_dir())

    mode = "[DRY RUN] " if args.dry_run else ""
    print(f"\n{mode}Migrating {len(env_dirs)} environment(s) in {envs_dir}\n")

    migrated = 0
    skipped  = 0
    for env_dir in env_dirs:
        result = migrate_env(env_dir, dry_run=args.dry_run)
        if result:
            migrated += 1
        else:
            skipped += 1

    print(f"\n{mode}Done: {migrated} migrated, {skipped} skipped.")
    if migrated and not args.dry_run:
        print("\nNext steps:")
        print("  git add envs/")
        print("  git commit -m 'refactor: migrate envs/ to per-service layout'")
        print("  git push")


if __name__ == "__main__":
    main()

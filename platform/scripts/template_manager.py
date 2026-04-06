"""
template_manager.py — Manage scaffold templates in the platform templates/ directory.

Templates are directories under templates/<name>/ containing at minimum a Dockerfile,
Jenkinsfile and service-manifest.yaml. An optional template.yaml stores metadata
(description, language). Git commits record who added or removed a template.

CLI usage:
  platform template list
  platform template info --name springboot
  platform template add  --name my-tpl --from-dir /path/to/dir [--description "..."]
  platform template remove --name my-tpl [--force]
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

from config import PlatformConfig
from compat import run
from output import out, step, success, warn, error_exit


class TemplateManager:
    def __init__(self, cfg: PlatformConfig, dry_run=False, json_output=False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.json_output = json_output

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────────────────

    def list_templates(self):
        templates = self._collect_all()
        if self.json_output:
            print(json.dumps(templates, indent=2))
            return
        if not templates:
            out("No templates found.")
            return
        col_w = [24, 14, 50]
        header = ["Template", "Language", "Description"]
        self._print_table(header, [
            [t["name"], t.get("language", "—"), t.get("description", "—")]
            for t in templates
        ], col_w)

    def info(self, name: str):
        tpl = self._load(name)
        if self.json_output:
            print(json.dumps(tpl, indent=2))
            return
        tpl_dir = self.cfg.templates_dir / name
        src_dir = tpl_dir / "src"
        if src_dir.exists():
            files = sorted(f.name for f in src_dir.iterdir())
            files_label = "src/"
        else:
            files = sorted(f.name for f in tpl_dir.iterdir() if f.name != "template.yaml")
            files_label = "(legacy) "
        has_build = (tpl_dir / "build.yaml").exists()
        print(f"\n  Template    : {name}")
        print(f"  {'─' * 40}")
        print(f"  Language    : {tpl.get('language', '—')}")
        print(f"  Description : {tpl.get('description', '—')}")
        if tpl.get("created_at"):
            print(f"  Created     : {tpl['created_at'][:10]}")
        if tpl.get("created_by"):
            print(f"  Added by    : {tpl['created_by']}")
        print(f"  Build cfg   : {'yes (build.yaml)' if has_build else 'no — shared lib falls back to template name'}")
        print(f"  Src files   : {files_label}{', '.join(files)}")
        print()

    def add(
        self,
        name: str,
        from_dir: str,
        description: str = "",
        language: str = "",
        force: bool = False,
    ):
        self._validate_name(name)
        source = Path(from_dir).expanduser().resolve()
        if not source.is_dir():
            error_exit(f"Source directory '{from_dir}' does not exist or is not a directory.")

        target = self.cfg.templates_dir / name
        if target.exists() and not force:
            error_exit(
                f"Template '{name}' already exists. Use --force to overwrite."
            )

        from identity import resolve_identity, format_disclaimer
        from output import confirm_with_actor
        identity = resolve_identity(self.cfg)
        actions = [
            f"Copy '{source}' → templates/{name}/",
            f"Write templates/{name}/template.yaml",
            f"Git commit: 'template: add template {name}'",
        ]
        if not self.dry_run and not self.json_output:
            confirm_with_actor(format_disclaimer(identity, actions), force=force)

        step(f"Copying template files from '{source}'")
        if not self.dry_run:
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)

            # Validate expected structure
            if not (target / "src").exists():
                warn(f"No src/ directory found — scaffold will use legacy flat layout.")
            if not (target / "build.yaml").exists():
                warn(f"No build.yaml found — Jenkins shared lib will fall back to template-name matching.")

            # Infer description from README.md if not provided
            readme = (target / "src" / "README.md") if (target / "src").exists() else (target / "README.md")
            if not description and readme.exists():
                first = readme.read_text(errors="replace").split("\n")[0]
                description = first.lstrip("# ").strip()

            # Write template.yaml
            meta = {
                "name":        name,
                "description": description,
                "language":    language,
                "created_at":  datetime.now(timezone.utc).isoformat(),
                "created_by":  identity.display_name,
            }
            (target / "template.yaml").write_text(
                yaml.dump(meta, default_flow_style=False, allow_unicode=True)
            )

            self._git_commit(f"template: add template '{name}'")

        result = {"name": name, "source": str(source), "status": "added"}
        if self.json_output:
            print(json.dumps(result, indent=2))
        else:
            success(f"Template '{name}' added.")
            print(f"  Source : {source}")
            print(f"  Path   : templates/{name}/")
        return result

    def remove(self, name: str, force: bool = False):
        target = self.cfg.templates_dir / name
        if not target.exists():
            error_exit(f"Template '{name}' not found.")

        # Warn if any service uses this template
        in_use = self._services_using(name)
        if in_use and not force:
            error_exit(
                f"Template '{name}' is used by service(s): {', '.join(in_use)}. "
                "Use --force to remove anyway."
            )

        from identity import resolve_identity, format_disclaimer
        from output import confirm_with_actor
        identity = resolve_identity(self.cfg)
        actions = [f"Delete templates/{name}/",
                   f"Git commit: 'template: remove template {name}'"]
        if in_use:
            actions.append(f"WARNING: used by {', '.join(in_use)} (--force)")
        if not self.dry_run and not self.json_output:
            confirm_with_actor(format_disclaimer(identity, actions), force=force)

        step(f"Removing template '{name}'")
        if not self.dry_run:
            shutil.rmtree(target)
            self._git_commit(f"template: remove template '{name}'")

        result = {"name": name, "status": "removed"}
        if self.json_output:
            print(json.dumps(result, indent=2))
        else:
            success(f"Template '{name}' removed.")
            if in_use:
                warn(f"Services still referencing this template: {', '.join(in_use)}")
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # PRIVATE
    # ─────────────────────────────────────────────────────────────────────────

    def _load(self, name: str) -> dict:
        target = self.cfg.templates_dir / name
        if not target.is_dir():
            error_exit(f"Template '{name}' not found.")
        meta_file = target / "template.yaml"
        if meta_file.exists():
            data = yaml.safe_load(meta_file.read_text()) or {}
        else:
            data = {"name": name}
            readme = target / "README.md"
            if readme.exists():
                first = readme.read_text(errors="replace").split("\n")[0]
                data["description"] = first.lstrip("# ").strip()
        data["name"] = name
        return data

    def _collect_all(self) -> list[dict]:
        if not self.cfg.templates_dir.exists():
            return []
        result = []
        for d in sorted(self.cfg.templates_dir.iterdir()):
            if d.is_dir():
                result.append(self._load(d.name))
        return result

    def _services_using(self, template_name: str) -> list[str]:
        """Return service names whose catalog entry references this template."""
        result = []
        for svc_name in self.cfg.list_service_names():
            try:
                catalog = self.cfg.load_service(svc_name)
                if catalog.get("template") == template_name:
                    result.append(svc_name)
            except Exception:
                pass
        return result

    def _validate_name(self, name: str):
        import re
        if not re.match(r"^[a-z][a-z0-9-]{0,48}[a-z0-9]$", name):
            error_exit(
                f"Invalid template name '{name}'. "
                "Use lowercase letters, digits and hyphens (2-50 chars)."
            )

    def _git_commit(self, message: str) -> bool:
        try:
            run(["git", "rev-parse", "--git-dir"],
                cwd=self.cfg.root, check=True, capture_output=True)
        except Exception:
            warn("Not a git repository — change not committed.")
            return False
        try:
            run(["git", "add", "templates/"],
                cwd=self.cfg.root, check=True, capture_output=True)
            run(["git", "commit", "-m", message],
                cwd=self.cfg.root, check=True, capture_output=True)
        except subprocess.CalledProcessError:
            warn(f"Could not auto-commit. Run: git add templates/ && git commit -m '{message}'")
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
            warn(f"Could not push to remote: {stderr}.")
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

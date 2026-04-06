"""release_notes.py — Display release notes from GitHub Releases."""

import json
import subprocess

import requests

from config import PlatformConfig
from output import out, warn, error_exit


class ReleaseNotesGenerator:
    def __init__(self, cfg: PlatformConfig, json_output=False):
        self.cfg = cfg
        self.json_output = json_output

    def show(self, service: str, version: str = None):
        if self.cfg.github_token:
            self._from_github(service, version)
        else:
            self._from_git_log(service, version)

    def _from_github(self, service, version):
        base = f"https://api.github.com/repos/{self.cfg.github_org}/{service}"
        headers = {
            "Authorization": f"token {self.cfg.github_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        if version:
            url = f"{base}/releases/tags/v{version}"
        else:
            url = f"{base}/releases/latest"
        resp = requests.get(url, headers=headers)
        if resp.status_code == 404:
            warn(f"No GitHub release found for {service}" + (f" v{version}" if version else ""))
            return
        data = resp.json()
        if self.json_output:
            print(json.dumps({
                "service": service,
                "version": data.get("tag_name"),
                "body": data.get("body"),
                "published_at": data.get("published_at"),
            }, indent=2))
        else:
            print(f"\n  {service} — {data.get('tag_name', '?')}")
            print(f"  Published : {data.get('published_at', '—')[:10]}")
            print(f"  {'─' * 50}")
            print(data.get("body", "(no release notes)"))
            print()

    def _from_git_log(self, service, version):
        warn("GITHUB_TOKEN not set — reading from local git log")
        try:
            log = subprocess.check_output(
                ["git", "log", "--oneline", "-20", "--", f"*{service}*"],
                cwd=self.cfg.root, text=True
            )
            if self.json_output:
                print(json.dumps({"service": service, "log": log}))
            else:
                print(f"\n  Recent commits for {service}:\n")
                print(log or "  (no commits found)")
        except subprocess.CalledProcessError:
            out("Could not read git log.")

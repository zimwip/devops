"""
identity.py — Resolve the acting identity from configured tokens.

Used to show a "changes will be performed on behalf of X" disclaimer
before any state-mutating operation.
"""

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

import requests


@dataclass
class ActorIdentity:
    """Consolidated identity of the person/service performing an action."""
    # GitHub
    github_login: Optional[str] = None
    github_name: Optional[str] = None
    github_email: Optional[str] = None
    # "missing" | "invalid" | "unverified" | "valid"
    github_token_state: str = "missing"

    # Jenkins
    jenkins_user: Optional[str] = None
    jenkins_url: Optional[str] = None
    # "missing" | "invalid" | "unverified" | "valid"
    jenkins_token_state: str = "missing"

    # Git (local config)
    git_name: Optional[str] = None
    git_email: Optional[str] = None

    # Warnings about token verification issues (not fatal — callers decide)
    warnings: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        """Best available human-readable name."""
        if self.github_name:
            return self.github_name
        if self.github_login:
            return self.github_login
        if self.git_name:
            return self.git_name
        if self.jenkins_user:
            return self.jenkins_user
        return "unknown"

    @property
    def display_email(self) -> str:
        if self.github_email:
            return self.github_email
        if self.git_email:
            return self.git_email
        return ""

    def as_dict(self) -> dict:
        return {
            "github_login": self.github_login,
            "github_name":  self.github_name,
            "github_email": self.github_email,
            "jenkins_user": self.jenkins_user,
            "jenkins_url":  self.jenkins_url,
            "git_name":     self.git_name,
            "git_email":    self.git_email,
            "display_name": self.display_name,
            "display_email": self.display_email,
            "warnings":     self.warnings,
        }


def resolve_identity(cfg) -> ActorIdentity:
    """
    Resolve the acting identity from all configured token sources.
    Never raises — always returns an ActorIdentity (possibly with warnings).
    """
    identity = ActorIdentity()

    # ── Git local config ───────────────────────────────────────────────────
    try:
        import subprocess
        name = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True, text=True, cwd=cfg.root,
        ).stdout.strip()
        email = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True, text=True, cwd=cfg.root,
        ).stdout.strip()
        identity.git_name  = name  or None
        identity.git_email = email or None
    except Exception:
        pass

    # ── GitHub token ───────────────────────────────────────────────────────
    github_token = cfg.github_token
    if github_token:
        try:
            resp = requests.get(
                f"{cfg.github_api_base}/user",
                headers={
                    "Authorization": f"token {github_token}",
                    "Accept": "application/vnd.github.v3+json",
                },
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                identity.github_login       = data.get("login")
                identity.github_name        = data.get("name")
                identity.github_email       = data.get("email")
                identity.github_token_state = "valid"
            elif resp.status_code == 401:
                identity.github_token_state = "invalid"
            else:
                identity.github_token_state = "unverified"
                identity.warnings.append(
                    f"GitHub identity check returned HTTP {resp.status_code} — "
                    "token validity could not be confirmed."
                )
        except requests.exceptions.Timeout:
            identity.github_token_state = "unverified"
            identity.warnings.append(
                "GitHub identity check timed out — token validity could not be confirmed."
            )
        except Exception as e:
            identity.github_token_state = "unverified"
            identity.warnings.append(f"GitHub identity check failed: {e}")
    # else: state stays "missing"

    # ── Jenkins token ──────────────────────────────────────────────────────
    jenkins_user  = cfg.jenkins_user
    jenkins_token = cfg.jenkins_token
    jenkins_url   = cfg.jenkins_url

    if jenkins_token and jenkins_user:
        try:
            resp = requests.get(
                f"{jenkins_url}/me/api/json",
                auth=(jenkins_user, jenkins_token),
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                identity.jenkins_user        = data.get("fullName") or jenkins_user
                identity.jenkins_url         = jenkins_url
                identity.jenkins_token_state = "valid"
            elif resp.status_code == 401:
                identity.jenkins_token_state = "invalid"
            else:
                # Jenkins responded — user exists, just couldn't fetch full name
                identity.jenkins_user        = jenkins_user
                identity.jenkins_url         = jenkins_url
                identity.jenkins_token_state = "unverified"
                identity.warnings.append(
                    f"Jenkins identity check returned HTTP {resp.status_code} — "
                    "token validity could not be confirmed."
                )
        except requests.exceptions.Timeout:
            identity.jenkins_user        = jenkins_user
            identity.jenkins_url         = jenkins_url
            identity.jenkins_token_state = "unverified"
            identity.warnings.append(
                "Jenkins identity check timed out — token validity could not be confirmed."
            )
        except Exception as e:
            identity.jenkins_user        = jenkins_user
            identity.jenkins_token_state = "unverified"
            identity.warnings.append(f"Jenkins identity check failed: {e}")
    # else: state stays "missing"

    return identity


def format_disclaimer(identity: ActorIdentity, actions: list[str]) -> str:
    """
    Format the confirmation disclaimer shown before a mutating operation.

    actions — list of human-readable strings describing what will happen,
               e.g. ["Create GitHub repo my-org/my-service",
                     "Register Jenkins pipeline my-service"]
    """
    lines = []
    lines.append("")
    lines.append("  ┌─ Confirmation required ────────────────────────────────────┐")
    lines.append("  │")
    lines.append("  │  The following changes will be performed on behalf of:")
    lines.append("  │")

    if identity.github_login:
        label = identity.github_name or identity.github_login
        lines.append(f"  │    GitHub   : {label} (@{identity.github_login})")
        if identity.github_email:
            lines.append(f"  │               {identity.github_email}")
    elif identity.github_token_state == "unverified":
        lines.append("  │    GitHub   : (token set — identity unverified)")
    else:
        lines.append("  │    GitHub   : (not configured)")

    if identity.jenkins_user:
        lines.append(f"  │    Jenkins  : {identity.jenkins_user}  ({identity.jenkins_url})")
    elif identity.jenkins_token_state == "unverified":
        lines.append("  │    Jenkins  : (token set — identity unverified)")
    else:
        lines.append("  │    Jenkins  : (not configured)")

    if identity.git_name or identity.git_email:
        git_id = " ".join(filter(None, [identity.git_name, f"<{identity.git_email}>" if identity.git_email else ""]))
        lines.append(f"  │    Git      : {git_id}")

    lines.append("  │")
    lines.append("  │  Actions:")
    for action in actions:
        lines.append(f"  │    · {action}")

    if identity.warnings:
        lines.append("  │")
        lines.append("  │  Warnings:")
        for w in identity.warnings:
            # Wrap long warnings
            for part in _wrap(w, 56):
                lines.append(f"  │    ! {part}")

    lines.append("  │")
    lines.append("  └────────────────────────────────────────────────────────────┘")
    lines.append("")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    """Simple word-wrap for disclaimer lines."""
    words = text.split()
    lines, current = [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            if current:
                lines.append(current)
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        lines.append(current)
    return lines or [""]

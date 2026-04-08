"""
history.py — Platform audit log.

Aggregates events from two sources:
  1. git log on envs/  — env lifecycle events (create, destroy, update)
  2. services[*].deployed_at in each versions.yaml — deployment events

Events are sorted chronologically (newest first) and can be filtered by
environment, service, actor, or event type.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import PlatformConfig


# ── Event model ───────────────────────────────────────────────────────────────

EVENT_TYPES = {
    "env_create":   "Environment created",
    "env_destroy":  "Environment destroyed",
    "env_update":   "Environment updated",
    "deploy":       "Service deployed",
    "service_reg":  "Service registered",
    "reset":        "Platform reset",
}

RESET_MARKER = "chore: platform reset"


@dataclass
class AuditEvent:
    timestamp: str                   # ISO-8601
    event_type: str                  # env_create | env_destroy | deploy | ...
    actor: str                       # who performed the action
    env: str                         # which environment
    service: Optional[str] = None    # which service (None for env-level events)
    version: Optional[str] = None    # which version was deployed
    image: Optional[str] = None      # full image reference
    commit: Optional[str] = None     # git commit SHA (when available)
    message: Optional[str] = None    # free-text detail
    platform: Optional[str] = None   # openshift | aws
    cluster: Optional[str] = None    # cluster name
    warning: bool = False            # True when commit message was not recognised

    @property
    def label(self) -> str:
        return EVENT_TYPES.get(self.event_type, self.event_type)

    @property
    def timestamp_dt(self) -> datetime:
        try:
            return datetime.fromisoformat(self.timestamp.replace("Z", "+00:00"))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None or k == "warning"}


# ── Collector ─────────────────────────────────────────────────────────────────

class HistoryCollector:
    def __init__(self, cfg: PlatformConfig):
        self.cfg = cfg

    def collect(
        self,
        env_filter: str | None = None,
        service_filter: str | None = None,
        actor_filter: str | None = None,
        event_type_filter: str | None = None,
        limit: int = 100,
        full: bool = False,
    ) -> list[AuditEvent]:
        """
        Return audit events in chronological order (oldest first, newest last).

        Ordering strategy:
          - Git-log events follow commit parent order (--topo-order --reverse):
            parent commits (older) appear before their children (newer). This is
            deterministic and immune to author-timestamp skew from rebases.
          - Snapshot-only events (versions.yaml entries with no corresponding
            git commit) are sorted by timestamp and appended at the end.
          - limit trims from the head (oldest events dropped) so that the most
            recent `limit` events are always included.
        """
        # ── Source 1: git log — already in topological order, newest first ────
        git_events = self._from_git_log(env_filter, full=full)

        # ── Source 2: versions.yaml snapshot ──────────────────────────────────
        snapshot_events = self._from_versions_snapshots(env_filter, service_filter)

        # ── Dedup: git events take precedence ─────────────────────────────────
        # Key does NOT include timestamp so that a git-log deploy event and its
        # matching versions.yaml entry (which may differ by a few seconds) are
        # recognised as the same event.
        seen: set[tuple] = set()
        ordered: list[AuditEvent] = []

        # content_keys: set of (env, service, version, event_type) tuples already
        # covered by a git commit — used to suppress matching snapshot entries.
        content_keys: set[tuple] = set()

        for e in git_events:
            # Each git commit is unique by its SHA; include it so that two
            # commits touching the same service (e.g. register→remove→register)
            # are never collapsed into one.
            sha_key = (e.commit or e.timestamp[:19], e.env, e.service or "", e.version or "", e.event_type)
            if sha_key not in seen:
                seen.add(sha_key)
                content_keys.add((e.env, e.service or "", e.version or "", e.event_type))
                ordered.append(e)

        # Snapshot events not already covered by a git commit.
        # Use content-based key (no SHA) so a deploy event in git log
        # suppresses its matching versions.yaml entry.
        snap_extras: list[AuditEvent] = []
        for e in snapshot_events:
            ck = (e.env, e.service or "", e.version or "", e.event_type)
            if ck not in content_keys:
                snap_extras.append(e)

        snap_extras.sort(key=lambda e: e.timestamp_dt)   # oldest first, matching git --reverse
        ordered.extend(snap_extras)

        # ── Filters ───────────────────────────────────────────────────────────
        if service_filter:
            ordered = [e for e in ordered if e.service == service_filter]
        if actor_filter:
            ordered = [e for e in ordered
                       if actor_filter.lower() in (e.actor or "").lower()]
        if event_type_filter:
            ordered = [e for e in ordered if e.event_type == event_type_filter]
        if env_filter:
            # Strict filter: keep only events whose env exactly matches.
            ordered = [e for e in ordered if e.env == env_filter]

        # Trim from the head so the most recent `limit` events are kept.
        return ordered[-limit:] if len(ordered) > limit else ordered

    # ── Git log ───────────────────────────────────────────────────────────────

    def _find_last_reset_sha(self) -> str | None:
        """
        Return the SHA of the most recent reset commit, or None if none exists.
        Scans all commits (no path filter) since the reset commit may only touch
        platform.yaml when envs/ is already empty.
        """
        try:
            result = subprocess.run(
                ["git", "log", "--format=%H|%s"],
                cwd=self.cfg.root, capture_output=True, text=True, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 1)
            if len(parts) == 2 and parts[1].strip() == RESET_MARKER:
                return parts[0].strip()
        return None

    def _from_git_log(self, env_filter: str | None, full: bool = False) -> list[AuditEvent]:
        """
        Parse git log on the envs/ directory for lifecycle events.

        full=False (default): scope to commits since the last reset commit.
        full=True: include all history, including pre-reset commits and reset
        events themselves.
        """
        events: list[AuditEvent] = []
        git_root = self.cfg.root

        # Build the git log command
        cmd = [
            "git", "log",
            "--topo-order",
            "--reverse",          # parent before child = oldest first
            "--pretty=format:%H|%aI|%ae|%s",
        ]

        if not full:
            reset_sha = self._find_last_reset_sha()
            if reset_sha:
                cmd.append(f"{reset_sha}..HEAD")

        cmd.extend(["--", "envs/"])

        try:
            result = subprocess.run(
                cmd,
                cwd=git_root, capture_output=True, text=True, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return events  # not a git repo or git not installed — skip silently

        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            commit_sha, timestamp, author_email, message = parts

            event = self._parse_git_message(
                commit_sha=commit_sha[:8],
                timestamp=timestamp,
                actor=author_email,
                message=message,
                env_filter=env_filter,
            )
            if event:
                events.append(event)

        return events

    def _parse_git_message(
        self,
        commit_sha: str,
        timestamp: str,
        actor: str,
        message: str,
        env_filter: str | None,
    ) -> AuditEvent | None:
        """
        Turn a git commit message into an AuditEvent.

        Supported prefixes:
          env:    create / destroy / update environment events
          deploy: deployment events  — "deploy: svc:ver → env [platform/cluster]"
          svc:    service register / remove — "svc: register service 'name'"
        """
        msg = message.strip()

        # ── env: ──────────────────────────────────────────────────────────────

        # env: create environment 'NAME'
        m = re.match(r"env: create (?:POC )?environment ['\"]?([^'\"]+)['\"]?", msg, re.I)
        if m:
            env_name = m.group(1).strip()
            if env_filter and env_name != env_filter:
                return None
            return AuditEvent(
                timestamp=timestamp, event_type="env_create",
                actor=actor, env=env_name, commit=commit_sha,
                message=msg,
            )

        # env: destroy POC environment 'NAME'
        m = re.match(r"env: destroy (?:POC )?environment ['\"]?([^'\"]+)['\"]?", msg, re.I)
        if m:
            env_name = m.group(1).strip()
            if env_filter and env_name != env_filter:
                return None
            return AuditEvent(
                timestamp=timestamp, event_type="env_destroy",
                actor=actor, env=env_name, commit=commit_sha,
                message=msg,
            )

        # generic env update (versions.yaml touched)
        if re.match(r"env:", msg, re.I):
            env_name = self._guess_env_from_message(msg)
            if env_filter and env_name and env_name != env_filter:
                return None
            return AuditEvent(
                timestamp=timestamp, event_type="env_update",
                actor=actor, env=env_name or "unknown", commit=commit_sha,
                message=msg,
            )

        # ── deploy: ───────────────────────────────────────────────────────────
        # Format: "deploy: <svc>:<version> → <env> [<platform>/<cluster>]"
        m = re.match(
            r"deploy:\s+([^:]+):([^\s]+)\s*[→>-]+\s*(\S+)(?:\s+\[([^\]]+)\])?",
            msg, re.I,
        )
        if m:
            svc_name = m.group(1).strip()
            version   = m.group(2).strip()
            env_name  = m.group(3).strip()
            detail    = m.group(4) or ""
            platform, cluster = (detail.split("/", 1) + [""])[:2] if detail else ("", "")
            if env_filter and env_name != env_filter:
                return None
            return AuditEvent(
                timestamp=timestamp, event_type="deploy",
                actor=actor, env=env_name, service=svc_name, version=version,
                platform=platform or None, cluster=cluster or None,
                commit=commit_sha, message=msg,
            )

        # ── svc: ──────────────────────────────────────────────────────────────
        # Format: "svc: register service 'NAME'" / "svc: remove service 'NAME'"
        m = re.match(r"svc:\s+(register|remove)\s+service\s+['\"]?([^'\"]+)['\"]?", msg, re.I)
        if m:
            action   = m.group(1).lower()
            svc_name = m.group(2).strip()
            # service_reg events are not bound to a specific env in the message;
            # they're filtered out when env_filter is set (env stays "—")
            if env_filter:
                return None
            return AuditEvent(
                timestamp=timestamp,
                event_type="service_reg",
                actor=actor,
                env="—",
                service=svc_name,
                commit=commit_sha,
                message=f"{action} service '{svc_name}'",
            )

        # ── Platform reset commit ─────────────────────────────────────────────
        if msg == RESET_MARKER:
            if env_filter:
                return None
            return AuditEvent(
                timestamp=timestamp, event_type="reset",
                actor=actor, env="platform", commit=commit_sha,
                message="Platform state cleared — git history preserved",
            )

        # ── chore: / feat: / fix: / refactor: (Conventional Commits on envs/) ───
        # Catch bootstrap and any other conventional-commit that touches envs/.
        # "chore: initial AP3 platform bootstrap" is the most important one.
        m = re.match(r"(chore|feat|fix|refactor|docs|perf|test):\s+(.+)", msg, re.I)
        if m:
            subject = m.group(2).strip()
            # Suppress env_filter for platform-wide commits (no specific env)
            if env_filter:
                return None
            return AuditEvent(
                timestamp=timestamp, event_type="env_update",
                actor=actor, env="platform", commit=commit_sha,
                message=subject,
            )

        # ── Final fallback: unrecognised commit that still touched envs/ ─────
        # Emit with warning=True so the UI can flag it visually.
        env_name = self._guess_env_from_message(msg)
        if env_filter and env_name != env_filter:
            return None
        return AuditEvent(
            timestamp=timestamp, event_type="env_update",
            actor=actor, env=env_name or "platform", commit=commit_sha,
            message=msg, warning=True,
        )

    def _guess_env_from_message(self, msg: str) -> str | None:
        # Look for known env names in the message
        for env in self.cfg.list_envs():
            if env in msg:
                return env
        return None

    # ── versions.yaml snapshots ───────────────────────────────────────────────

    def _from_versions_snapshots(
        self,
        env_filter: str | None,
        service_filter: str | None,
    ) -> list[AuditEvent]:
        """
        Extract deployment events from the current state of all versions.yaml files.
        Each service entry with a deployed_at timestamp is one event.
        """
        events: list[AuditEvent] = []
        envs = [env_filter] if env_filter else self.cfg.list_envs()

        for env_name in envs:
            try:
                data = self.cfg.load_versions(env_name)
            except FileNotFoundError:
                continue

            meta = data.get("_meta", {})
            platform = meta.get("platform", "openshift")
            cluster = meta.get("cluster")
            env_actor = meta.get("updated_by", "unknown")
            env_type = meta.get("env_type", "fixed")

            # Env-level create event from _meta — only for POC environments.
            # Fixed envs are bootstrapped once and never re-created; their
            # versions.yaml is updated on every deploy so emitting env_create on
            # every snapshot would flood the history with false events.
            env_updated = meta.get("updated_at")
            if env_updated and env_type == "poc" and meta.get("commit") != "bootstrap":
                if not env_filter or env_name == env_filter:
                    events.append(AuditEvent(
                        timestamp=env_updated,
                        event_type="env_create",
                        actor=env_actor,
                        env=env_name,
                        platform=platform,
                        cluster=cluster,
                        message=f"Created via platform-cli (base: {meta.get('base_env', '—')})",
                    ))

            # Per-service deployment events.
            # Skip any entry without a deployed_by field AND commit==bootstrap —
            # those are pre-populated stubs, not real events.
            # Demo entries (commit==demo) are shown but labelled.
            for svc_name, svc in (data.get("services") or {}).items():
                if service_filter and svc_name != service_filter:
                    continue
                deployed_at = svc.get("deployed_at")
                if not deployed_at:
                    continue
                is_demo = svc.get("demo", False) or meta.get("commit") == "demo"
                # Skip bootstrap stubs silently
                if not svc.get("deployed_by") and meta.get("commit") == "bootstrap":
                    continue
                events.append(AuditEvent(
                    timestamp=deployed_at,
                    event_type="deploy",
                    actor=svc.get("deployed_by") or env_actor,
                    env=env_name,
                    service=svc_name,
                    version=svc.get("version"),
                    image=svc.get("image"),
                    platform=platform,
                    cluster=cluster,
                    message="[demo data]" if is_demo else None,
                ))

        return events


# ── CLI formatter ─────────────────────────────────────────────────────────────

def format_history_table(events: list[AuditEvent]) -> str:
    """Render events as a fixed-width CLI table, newest first."""
    if not events:
        return "\n  No history found.\n"

    col_w = [20, 14, 18, 20, 14, 26]
    header = ["Timestamp", "Type", "Env", "Actor", "Service", "Version / detail"]

    def fmt(row):
        return "  " + "  ".join(str(v)[:w].ljust(w) for v, w in zip(row, col_w))

    lines = ["\n", fmt(header), "  " + "  ".join("-" * w for w in col_w)]
    for e in reversed(events):
        detail = e.version or e.message or ""
        if e.cluster:
            detail = f"{e.version or ''} [{e.cluster}]".strip()
        lines.append(fmt([
            e.timestamp[:19].replace("T", " "),
            e.event_type.replace("_", " "),
            e.env[:18],
            (e.actor or "unknown")[:18],
            (e.service or "—")[:14],
            detail[:26],
        ]))
    lines.append("")
    return "\n".join(lines)

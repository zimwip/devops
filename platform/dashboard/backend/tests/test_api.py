"""
tests/test_api.py
FastAPI backend unit tests — run with: pytest tests/ -v

These tests work against the real envs/ directory in the repo.
No mocking needed: the platform logic reads YAML files directly.
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Resolve paths so pytest can import backend and scripts
REPO_ROOT = Path(__file__).parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(REPO_ROOT / "dashboard" / "backend"))

from app import app

client = TestClient(app)


# ── Environments ──────────────────────────────────────────────────────────────

class TestEnvEndpoints:
    def test_list_envs_returns_list(self):
        r = client.get("/api/envs")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) >= 3  # dev, val, prod always present

    def test_list_envs_contains_fixed_envs(self):
        r = client.get("/api/envs")
        names = [e["name"] for e in r.json()]
        for expected in ("dev", "val", "prod"):
            assert expected in names, f"Expected env '{expected}' in list"

    def test_get_env_dev(self):
        r = client.get("/api/envs/dev")
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "dev"
        assert data["type"] in ("fixed", "poc")
        assert "services" in data

    def test_get_env_not_found(self):
        r = client.get("/api/envs/nonexistent-env-xyz")
        assert r.status_code == 404

    def test_env_diff_returns_list(self):
        r = client.get("/api/envs/dev/diff/prod")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        for entry in data:
            assert "service" in entry
            assert "changed" in entry

    def test_env_diff_missing_env(self):
        r = client.get("/api/envs/dev/diff/does-not-exist")
        assert r.status_code == 404

    def test_destroy_fixed_env_forbidden(self):
        r = client.delete("/api/envs/prod")
        assert r.status_code in (403, 404)


# ── Services ──────────────────────────────────────────────────────────────────

class TestServiceEndpoints:
    def test_list_services_returns_list(self):
        r = client.get("/api/services")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_service_has_required_fields(self):
        r = client.get("/api/services")
        services = r.json()
        if services:
            svc = services[0]
            assert "name" in svc
            assert "versions" in svc

    def test_get_service_not_found(self):
        r = client.get("/api/services/this-service-does-not-exist")
        assert r.status_code == 404

    def test_create_service_missing_fields(self):
        r = client.post("/api/services", json={"name": "incomplete"})
        assert r.status_code == 422  # FastAPI validation error

    def test_create_service_invalid_name_rejected(self):
        """The service_creator validates kebab-case names."""
        r = client.post("/api/services", json={
            "name": "INVALID NAME WITH SPACES",
            "template": "springboot",
            "owner": "team-x",
            "skip_github": True,
            "skip_jenkins": True,
        })
        # Either 422 (validation) or 400 (business logic)
        assert r.status_code in (400, 422)


# ── Deployments ───────────────────────────────────────────────────────────────

class TestDeployEndpoints:
    def test_create_service_hosted_list(self):
        """GET /api/services/hosted should return a list of strings."""
        r = client.get("/api/services/hosted")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_create_service_invalid_source_mode(self):
        r = client.post("/api/services", json={
            "name": "my-svc", "owner": "team", "source_mode": "invalid-mode",
        })
        assert r.status_code == 422

    def test_create_service_valid_schema_accepted(self):
        """All three source_mode values must be accepted when dry_run=true (no disk writes)."""
        for mode, extra in [
            ("template",  {"template": "springboot"}),
            ("fork",      {"fork_from": "some-service"}),
            ("external",  {"external_repo_url": "https://github.com/org/repo.git"}),
        ]:
            r = client.post("/api/services", json={
                "name": "my-svc", "owner": "team",
                "source_mode": mode, **extra,
                "dry_run": True, "force": True,
            })
            assert r.status_code != 422, \
                f"source_mode='{mode}' unexpectedly rejected by schema: {r.json()}"

    def test_deploy_missing_fields_rejected(self):
        r = client.post("/api/deploy", json={"env": "dev"})
        assert r.status_code == 422

    def test_deploy_unknown_env(self):
        r = client.post("/api/deploy", json={
            "env": "nonexistent-xyz",
            "service": "service-auth",
            "version": "1.0.0",
        })
        # Should fail gracefully — env not found
        assert r.status_code in (400, 404, 500)


# ── Templates ─────────────────────────────────────────────────────────────────

class TestTemplateEndpoints:
    def test_list_templates_returns_list(self):
        r = client.get("/api/templates")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)

    def test_known_templates_present(self):
        r = client.get("/api/templates")
        ids = [t["id"] for t in r.json()]
        for expected in ("springboot", "react", "python-api"):
            assert expected in ids, f"Template '{expected}' not found"

    def test_template_has_description(self):
        r = client.get("/api/templates")
        for t in r.json():
            assert "id" in t
            assert "description" in t


# ── Platform / cluster ────────────────────────────────────────────────────────

class TestPlatformAndCluster:
    def test_env_has_platform_field(self):
        """Every environment must expose a 'platform' field."""
        r = client.get("/api/envs")
        for env in r.json():
            assert "platform" in env, f"Env '{env['name']}' is missing 'platform' field"
            assert env["platform"] in ("openshift", "aws", "unknown"), \
                f"Unexpected platform value: {env['platform']}"

    def test_dev_env_is_openshift(self):
        """The bootstrap dev environment should be openshift."""
        r = client.get("/api/envs/dev")
        assert r.status_code == 200
        assert r.json()["platform"] == "openshift"

    def test_create_env_with_platform_openshift(self):
        """POC creation with explicit platform=openshift stores it correctly."""
        r = client.post("/api/envs", json={
            "name": "platform-test-ocp",
            "base": "dev",
            "platform": "openshift",
            "owner": "test",
            "force": True,
        })
        assert r.status_code in (201, 409)
        if r.status_code == 201:
            created = r.json()
            assert created["status"] == "created"
            env_name = created["name"]
            # Fetch the env detail to verify platform was stored
            detail = client.get(f"/api/envs/{env_name}").json()
            assert detail["platform"] == "openshift"
            # Cleanup
            client.delete(f"/api/envs/{env_name}")

    def test_create_env_with_platform_aws(self):
        """POC creation with explicit platform=aws stores it correctly."""
        r = client.post("/api/envs", json={
            "name": "platform-test-aws",
            "base": "dev",
            "platform": "aws",
            "cluster": "eks-dev",
            "owner": "test",
            "force": True,
        })
        assert r.status_code in (201, 409)
        if r.status_code == 201:
            created = r.json()
            assert created["status"] == "created"
            env_name = created["name"]
            detail = client.get(f"/api/envs/{env_name}").json()
            assert detail["platform"] == "aws"
            # Cleanup
            client.delete(f"/api/envs/{env_name}")

    def test_create_env_with_cluster(self):
        """POC creation with explicit cluster stores it and derives platform."""
        r = client.post("/api/envs", json={
            "name": "cluster-test",
            "base": "dev",
            "cluster": "openshift-dev",
            "owner": "test",
            "force": True,
        })
        assert r.status_code in (201, 409)
        if r.status_code == 201:
            created = r.json()
            assert created["status"] == "created"
            env_name = created["name"]
            detail = client.get(f"/api/envs/{env_name}").json()
            assert detail["cluster"] == "openshift-dev"
            client.delete(f"/api/envs/{env_name}")

    def test_create_env_with_namespace(self):
        """POC creation with explicit namespace records namespace_provided flag."""
        import yaml
        from pathlib import Path
        r = client.post("/api/envs", json={
            "name": "ns-provided-test",
            "base": "dev",
            "namespace": "my-existing-ns",
            "owner": "test",
            "force": True,
        })
        assert r.status_code in (201, 409)
        if r.status_code == 201:
            env_name = r.json()["name"]
            # Check the versions.yaml has namespace_provided: true
            root = Path(__file__).parent.parent.parent.parent.parent
            vpath = root / "envs" / env_name / "versions.yaml"
            if vpath.exists():
                data = yaml.safe_load(vpath.read_text())
                assert data["_meta"]["namespace"] == "my-existing-ns"
                assert data["_meta"]["namespace_provided"] is True
            client.delete(f"/api/envs/{env_name}")

    def test_deploy_request_accepts_platform_override(self):
        """Deploy endpoint should accept an optional platform field."""
        r = client.post("/api/deploy", json={
            "env": "nonexistent-xyz",
            "service": "service-auth",
            "version": "1.0.0",
            "platform": "openshift",
        })
        # Should fail on env-not-found, not on schema validation
        assert r.status_code in (400, 404, 500)

    def test_deploy_request_invalid_platform_rejected(self):
        """Deploy endpoint should reject unknown platform values."""
        r = client.post("/api/deploy", json={
            "env": "dev",
            "service": "service-auth",
            "version": "1.0.0",
            "platform": "gcp",   # not valid
        })
        assert r.status_code == 422

    def test_env_diff_shows_platform_context(self):
        """Diff endpoint should return entries for all known services."""
        r = client.get("/api/envs/dev/diff/prod")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        # Every entry must have both env keys
        for entry in data:
            assert "dev" in entry
            assert "prod" in entry
            assert "changed" in entry


# ── Root / health ─────────────────────────────────────────────────────────────

class TestRoot:
    def test_openapi_schema_available(self):
        r = client.get("/openapi.json")
        assert r.status_code == 200
        schema = r.json()
        assert schema["info"]["title"] == "Platform Dashboard API"

    def test_identity_endpoint_returns_structure(self):
        """Identity endpoint must return the expected fields."""
        r = client.get("/api/identity")
        assert r.status_code == 200
        data = r.json()
        assert "display_name" in data
        assert "warnings" in data
        assert isinstance(data["warnings"], list)
        # display_name is always populated (may be "unknown")
        assert data["display_name"]

    def test_docs_available(self):
        r = client.get("/docs")
        assert r.status_code == 200

    def test_redoc_available(self):
        r = client.get("/redoc")
        assert r.status_code == 200


# ── History ───────────────────────────────────────────────────────────────────

class TestHistoryEndpoint:
    def test_history_returns_list(self):
        r = client.get("/api/history")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_history_event_has_required_fields(self):
        r = client.get("/api/history")
        events = r.json()
        if events:
            e = events[0]
            for f in ("timestamp", "event_type", "label", "actor", "env"):
                assert f in e, f"Missing field '{f}' in history event"

    def test_history_sorted_newest_first(self):
        r = client.get("/api/history")
        events = r.json()
        timestamps = [e["timestamp"] for e in events if e.get("timestamp")]
        assert timestamps == sorted(timestamps, reverse=True), \
            "History events should be sorted newest first"

    def test_history_filter_by_env(self):
        r = client.get("/api/history?env=dev")
        assert r.status_code == 200
        # All returned events must be for env=dev
        # (git log events that don't parse cleanly are excluded by the filter)
        for e in r.json():
            assert e["env"] == "dev", f"Expected env=dev, got {e['env']}"

    def test_history_filter_by_type(self):
        r = client.get("/api/history?type=deploy")
        assert r.status_code == 200
        for e in r.json():
            assert e["event_type"] == "deploy"

    def test_history_limit_respected(self):
        r = client.get("/api/history?limit=3")
        assert r.status_code == 200
        assert len(r.json()) <= 3

    def test_history_filter_by_service(self):
        r = client.get("/api/history?service=service-auth")
        assert r.status_code == 200
        for e in r.json():
            if e.get("service"):
                assert e["service"] == "service-auth"

    def test_history_deploy_events_have_version(self):
        r = client.get("/api/history?type=deploy")
        for e in r.json():
            assert e.get("version") is not None, \
                f"Deploy event missing version: {e}"


# ── TTL / extend ──────────────────────────────────────────────────────────────

class TestTTLAndExtend:
    def test_create_env_ttl_cap_365(self):
        """TTL over 365 is rejected by the Pydantic validator."""
        r = client.post("/api/envs", json={
            "name": "ttl-overflow-test",
            "base": "dev",
            "ttl_days": 400,
            "owner": "test",
            "force": True,
        })
        assert r.status_code == 422, "TTL > 365 should be rejected"

    def test_create_env_ttl_max_365_accepted(self):
        """TTL of exactly 365 is valid."""
        r = client.post("/api/envs", json={
            "name": "ttl-max-test",
            "base": "dev",
            "ttl_days": 365,
            "owner": "test",
            "force": True,
        })
        assert r.status_code in (201, 409)
        if r.status_code == 201:
            client.delete(f"/api/envs/{r.json()['name']}")

    def test_extend_env_ttl(self):
        """Extending a POC environment moves expires_at forward."""
        from datetime import datetime, timezone, timedelta
        # Create a short-lived POC
        r = client.post("/api/envs", json={
            "name": "extend-test",
            "base": "dev",
            "ttl_days": 1,
            "owner": "test",
            "force": True,
        })
        assert r.status_code in (201, 409)
        env_name = r.json()["name"] if r.status_code == 201 else "poc-extend-test-20260403"

        if r.status_code == 201:
            # Get current expiry
            detail = client.get(f"/api/envs/{env_name}").json()
            old_expires = detail.get("expires_at", "")

            # Extend by 14 days
            ext = client.post(f"/api/envs/{env_name}/extend", json={"ttl_days": 14})
            assert ext.status_code == 200
            data = ext.json()
            assert data["status"] == "extended"
            assert data["days_remaining"] > 0
            assert data["expires_at"] > old_expires

            # Cleanup
            client.delete(f"/api/envs/{env_name}")

    def test_extend_fixed_env_rejected(self):
        """Cannot extend a fixed environment (no TTL)."""
        r = client.post("/api/envs/prod/extend", json={"ttl_days": 14})
        assert r.status_code == 400

    def test_extend_nonexistent_env(self):
        r = client.post("/api/envs/this-does-not-exist/extend", json={"ttl_days": 7})
        assert r.status_code == 404

    def test_env_summary_has_expiry_status(self):
        """EnvSummary should include expiry_status field for POC envs."""
        # List all envs and check POC ones have expiry_status
        r = client.get("/api/envs")
        for env in r.json():
            if env["type"] == "poc":
                assert "expiry_status" in env, \
                    f"POC env '{env['name']}' missing expiry_status"
                assert env["expiry_status"] in ("ok", "warning", "expired", "unknown", None)


# ── Clusters ──────────────────────────────────────────────────────────────────

class TestClusterEndpoints:
    def test_list_clusters_returns_list(self):
        r = client.get("/api/clusters")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_cluster_has_required_fields(self):
        r = client.get("/api/clusters")
        clusters = r.json()
        if clusters:
            for field in ("name", "platform", "registry", "helm_values_suffix", "in_use"):
                assert field in clusters[0], f"Missing field '{field}'"

    def test_get_cluster_not_found(self):
        r = client.get("/api/clusters/nonexistent-xyz")
        assert r.status_code == 404

    def test_add_openshift_cluster(self):
        r = client.post("/api/clusters", json={
            "name":     "test-ocp-cluster",
            "platform": "openshift",
            "api_url":  "https://api.test.internal:6443",
            "context":  "test-ocp",
            "registry": "registry.internal",
            "helm_values_suffix": "test",
        })
        assert r.status_code in (201, 409)
        if r.status_code == 201:
            data = r.json()
            assert data["name"]     == "test-ocp-cluster"
            assert data["platform"] == "openshift"
            assert data["api_url"]  == "https://api.test.internal:6443"
            # Cleanup
            client.delete("/api/clusters/test-ocp-cluster?force=true")

    def test_add_aws_cluster(self):
        r = client.post("/api/clusters", json={
            "name":         "test-eks-cluster",
            "platform":     "aws",
            "region":       "eu-west-1",
            "cluster_name": "my-eks",
            "registry":     "123456789.dkr.ecr.eu-west-1.amazonaws.com",
            "helm_values_suffix": "test",
        })
        assert r.status_code in (201, 409)
        if r.status_code == 201:
            data = r.json()
            assert data["platform"]     == "aws"
            assert data["region"]       == "eu-west-1"
            assert data["cluster_name"] == "my-eks"
            # Cleanup
            client.delete("/api/clusters/test-eks-cluster?force=true")

    def test_add_cluster_invalid_platform(self):
        r = client.post("/api/clusters", json={
            "name": "bad-cluster", "platform": "gcp",
        })
        assert r.status_code == 422

    def test_delete_cluster_not_found(self):
        r = client.delete("/api/clusters/nonexistent-xyz")
        assert r.status_code == 404

    def test_cluster_in_use_field(self):
        """Clusters used by environments should list those envs in in_use."""
        r = client.get("/api/clusters")
        clusters = r.json()
        # Find one that's referenced by an env
        used = [c for c in clusters if c.get("in_use")]
        if used:
            c = used[0]
            assert isinstance(c["in_use"], list)
            assert len(c["in_use"]) > 0

    def test_get_cluster_detail(self):
        r = client.get("/api/clusters")
        if not r.json():
            return
        name = r.json()[0]["name"]
        r2 = client.get(f"/api/clusters/{name}")
        assert r2.status_code == 200
        assert r2.json()["name"] == name

    def test_update_cluster(self):
        """PUT /api/clusters/{name} should update an existing profile."""
        # First create one
        add = client.post("/api/clusters", json={
            "name": "update-test-cluster", "platform": "openshift",
            "api_url": "https://old.internal:6443", "context": "old",
            "registry": "registry.internal", "helm_values_suffix": "old",
        })
        assert add.status_code in (201, 409)

        upd = client.put("/api/clusters/update-test-cluster", json={
            "name": "update-test-cluster", "platform": "openshift",
            "api_url": "https://new.internal:6443", "context": "new",
            "registry": "registry.internal", "helm_values_suffix": "new",
        })
        assert upd.status_code == 200
        assert upd.json()["api_url"] == "https://new.internal:6443"
        assert upd.json()["helm_values_suffix"] == "new"

        # Cleanup
        client.delete("/api/clusters/update-test-cluster?force=true")


# ── Platform config ────────────────────────────────────────────────────────────

class TestPlatformConfigEndpoints:
    def test_get_platform_config(self):
        r = client.get("/api/platform/config")
        assert r.status_code == 200
        data = r.json()
        assert "github_url"          in data
        assert "github_account_type" in data
        assert "github_org"          in data
        assert "jenkins_url"         in data
        assert "github_token_set"    in data
        assert "jenkins_token_set"   in data
        assert isinstance(data["github_token_set"], bool)

    def test_platform_config_has_defaults(self):
        r = client.get("/api/platform/config")
        data = r.json()
        assert data["github_account_type"] in ("org", "user")
        assert data["github_url"].startswith("http")
        assert data["jenkins_url"].startswith("http")

    def test_patch_platform_config_github_org(self):
        original = client.get("/api/platform/config").json()["github_org"]
        r = client.patch("/api/platform/config",
                         json={"github_org": "test-org-update"})
        assert r.status_code == 200
        assert r.json()["github_org"] == "test-org-update"
        # Restore
        client.patch("/api/platform/config", json={"github_org": original})

    def test_patch_platform_config_invalid_account_type(self):
        r = client.patch("/api/platform/config",
                         json={"github_account_type": "enterprise"})
        assert r.status_code == 422

    def test_patch_platform_config_partial(self):
        """Partial update must not overwrite other fields."""
        before = client.get("/api/platform/config").json()
        r = client.patch("/api/platform/config",
                         json={"jenkins_url": "https://jenkins-test.internal"})
        assert r.status_code == 200
        after = r.json()
        assert after["jenkins_url"]  == "https://jenkins-test.internal"
        assert after["github_org"]   == before["github_org"]   # unchanged
        assert after["github_url"]   == before["github_url"]   # unchanged
        # Restore
        client.patch("/api/platform/config",
                     json={"jenkins_url": before["jenkins_url"]})

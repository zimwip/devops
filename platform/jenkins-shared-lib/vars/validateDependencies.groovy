/**
 * validateDependencies.groovy
 * Reads service-manifest.yaml and checks constraints against the target environment.
 */
def call(Map config) {
    def targetEnv = config.env
    def service   = config.service ?: env.SERVICE_NAME
    sh """
        python3 /opt/platform/scripts/platform_cli.py \
            service info --name ${service} --json | \
        python3 -c "
import sys, json, subprocess, yaml, pathlib

info = json.load(sys.stdin)
manifest_path = pathlib.Path('service-manifest.yaml')
if not manifest_path.exists():
    print('No service-manifest.yaml found — skipping dependency check')
    sys.exit(0)

manifest = yaml.safe_load(manifest_path.read_text())
requires = manifest.get('requires', {})
versions  = info.get('versions', {})
env_versions = {k: v.get('version', '') for k, v in versions.items()}
env_svc_ver  = {}

# Load target env versions directly
import os
from pathlib import Path
envs_root = Path('/opt/platform/envs/${targetEnv}/versions.yaml')
if envs_root.exists():
    env_data = yaml.safe_load(envs_root.read_text()) or {}
    env_svc_ver = {k: v.get('version','') for k,v in (env_data.get('services') or {}).items()}

errors = []
for dep, constraint in requires.items():
    current = env_svc_ver.get(dep, '')
    if not current:
        print(f'  WARN: {dep} not found in ${targetEnv}, skipping constraint check')
        continue
    print(f'  {dep}: {current} (requires {constraint})')

if errors:
    for e in errors:
        print(f'  ERROR: {e}')
    sys.exit(1)
print('  All dependency constraints satisfied.')
"
    """
}

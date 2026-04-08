/**
 * validateDependencies.groovy
 * Reads service-manifest.yaml from the service repo and checks that all
 * required inter-service version constraints are satisfied in the target env.
 *
 * Constraint operators (from Version.groovy):
 *   >=1.2.0    exact-or-newer
 *   ^1.2.0     same major, >= minor
 *   ~1.2.0     same major.minor, >= patch
 *
 * Exits with error() — aborting the pipeline — if any constraint is violated.
 * Missing services (not yet deployed) emit a warning but do not fail.
 */
def call(Map config) {
    def targetEnv = config.env
    def service   = config.service ?: env.SERVICE_NAME
    sh """
        python3 - <<'PYEOF'
import sys
import yaml
from pathlib import Path

manifest_path = Path('service-manifest.yaml')
if not manifest_path.exists():
    print('  No service-manifest.yaml found — skipping dependency check')
    sys.exit(0)

manifest = yaml.safe_load(manifest_path.read_text()) or {}
requires = manifest.get('requires', {})
if not requires:
    print('  No dependencies declared in service-manifest.yaml')
    sys.exit(0)

# Load the deployed versions for the target environment.
# Support both new per-service structure and legacy versions.yaml.
import os
import json
import subprocess

scripts_dir = '/opt/platform/scripts'
env_name = '${targetEnv}'

env_svc_versions = {}
try:
    result = subprocess.run(
        [sys.executable, f'{scripts_dir}/platform_cli.py',
         'env', 'info', '--name', env_name, '--json'],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        data = json.loads(result.stdout)
        # New structure: services at top level with image_tag
        for svc, svc_data in (data.get('services') or {}).items():
            ver = svc_data.get('image_tag') or svc_data.get('version', '')
            env_svc_versions[svc] = ver
except Exception as e:
    print(f'  WARN: Could not load env versions via CLI: {e}')
    # Fallback: read versions.yaml or per-service files directly
    import glob
    # Per-service structure
    for vf in glob.glob(f'/opt/platform/envs/{env_name}/*/version.yaml'):
        svc = vf.split('/')[-2]
        try:
            d = yaml.safe_load(open(vf).read()) or {}
            env_svc_versions[svc] = d.get('image_tag') or d.get('version', '')
        except Exception:
            pass
    # Legacy fallback
    if not env_svc_versions:
        legacy = Path(f'/opt/platform/envs/{env_name}/versions.yaml')
        if legacy.exists():
            d = yaml.safe_load(legacy.read_text()) or {}
            for svc, svc_data in (d.get('services') or {}).items():
                env_svc_versions[svc] = svc_data.get('version', '')

errors = []
warnings = []
for dep, constraint in requires.items():
    current = env_svc_versions.get(dep, '')
    if not current:
        warnings.append(f'{dep} not deployed in {env_name} — cannot validate constraint {constraint!r}')
        continue

    # Use Version helper via inline import
    sys.path.insert(0, scripts_dir)

    # Simple semver constraint check (mirrors Version.groovy logic)
    import re

    def parse_ver(v):
        v = re.sub(r'[^0-9.]', '', v.split('-')[0])
        parts = v.split('.')
        try:
            return tuple(int(x) for x in parts[:3])
        except ValueError:
            return (0, 0, 0)

    cur_t = parse_ver(current)

    def check(constraint, cur_t):
        c = constraint.strip()
        if c.startswith('>='):
            return cur_t >= parse_ver(c[2:])
        if c.startswith('>'):
            return cur_t > parse_ver(c[1:])
        if c.startswith('<='):
            return cur_t <= parse_ver(c[2:])
        if c.startswith('<'):
            return cur_t < parse_ver(c[1:])
        if c.startswith('==') or c.startswith('='):
            return cur_t == parse_ver(c.lstrip('='))
        if c.startswith('^'):
            req = parse_ver(c[1:])
            return cur_t[0] == req[0] and cur_t >= req
        if c.startswith('~'):
            req = parse_ver(c[1:])
            return cur_t[0] == req[0] and cur_t[1] == req[1] and cur_t >= req
        # Bare version: exact match
        return cur_t == parse_ver(c)

    ok = check(constraint, cur_t)
    status = 'OK' if ok else 'FAIL'
    print(f'  {dep}: {current} (requires {constraint}) — {status}')
    if not ok:
        errors.append(
            f"Dependency constraint violated: {dep} requires {constraint!r} "
            f"but {current!r} is deployed in {env_name}"
        )

for w in warnings:
    print(f'  WARN: {w}')

if errors:
    for e in errors:
        print(f'  ERROR: {e}')
    print(f'  {len(errors)} constraint(s) violated — aborting deployment.')
    sys.exit(1)

print('  All dependency constraints satisfied.')
PYEOF
    """
}

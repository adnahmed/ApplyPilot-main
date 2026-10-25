"""Initialize missing local service configuration while preserving existing values."""
import os
from pathlib import Path
import secrets
import shutil

from dotenv import dotenv_values, set_key
from applypilot import config
from applypilot.orchestration.settings import load


def fill_missing(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    saved = dotenv_values(path, encoding='utf-8-sig', interpolate=False)
    for name, value in values.items():
        if not saved.get(name):
            set_key(path, name, value)
    return dotenv_values(path, encoding='utf-8-sig', interpolate=False)


guides = Path(__file__).resolve().parents[1] / 'docs' / 'guides'
gateway_env = guides / '.env'
if not gateway_env.exists():
    shutil.copyfile(guides / '.env.example', gateway_env)
    gateway_env.chmod(0o600)
gateway = fill_missing(gateway_env, {
    key: os.environ.get(key) or secrets.token_urlsafe(36)
    for key in ('POSTGRES_PASSWORD', 'TEMPORAL_POSTGRES_PASSWORD', 'GRAFANA_ADMIN_PASSWORD')
})
service_env = Path(os.environ.get('LITELLM_ENV_FILE') or gateway.get('LITELLM_ENV_FILE') or '.env').expanduser()
if not service_env.is_absolute():
    service_env = guides / service_env
service = fill_missing(service_env, {
    key: os.environ.get(key) or secrets.token_urlsafe(36)
    for key in ('LITELLM_MASTER_KEY', 'WARP_EGRESS_FINGERPRINT_SECRET', 'WARP_EGRESS_ADMIN_TOKEN')
})
config.ensure_dirs()
inherited_identity = {key: os.environ.get(key) for key in (
    'APPLYPILOT_TEMPORAL_NAMESPACE', 'APPLYPILOT_TEMPORAL_WORKSPACE')}
config.load_env()
policy = load()
identity = {}
for key, value, default in (
    ('APPLYPILOT_TEMPORAL_NAMESPACE', policy.namespace, 'applypilot'),
    ('APPLYPILOT_TEMPORAL_WORKSPACE', policy.workspace, 'default'),
):
    supplied = os.environ.get(key)
    compose_value = inherited_identity[key] or gateway.get(key)
    if compose_value and (supplied or value != default) and compose_value != value:
        raise ValueError(key + ' differs between the application and Docker configuration')
    identity[key] = compose_value or value
fill_missing(gateway_env, identity)
policy_file = config.APP_DIR / 'runtime_policy.yaml'
if policy_file.is_file():
    fill_missing(gateway_env, {'APPLYPILOT_TEMPORAL_POLICY_FILE': policy_file.as_posix()})
fill_missing(config.ENV_PATH, {
    'LLM_URL': 'http://127.0.0.1:4000/v1',
    'LLM_API_KEY': service['LITELLM_MASTER_KEY'],
    'SEARXNG_URL': 'http://127.0.0.1:' + (gateway.get('SEARXNG_PORT') or '8888'),
    'FLARESOLVERR_URL': 'http://127.0.0.1:' + (gateway.get('FLARESOLVERR_PORT') or '8191'),
    'APPLYPILOT_TEMPORAL_ADDRESS': '127.0.0.1:7233',
    **identity,
})
print('Local service configuration ready; existing credentials and application data preserved.')

"""One credential choice for direct native configuration and model dispatch."""
import json
import math
import time

from app.integrations.config_transactions import ConfigurationError


def account_status(account):
    """Current display state, without changing the authorization receipt."""
    status = account.get('status')
    if account.get('kind') != 'oauth' or status not in ('authorized', 'authorizing'):
        return status
    expiry = account.get('expires_at' if status == 'authorized' else 'authorization_expires_at')
    if expiry is None:
        return status
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
        return 'error'
    return 'expired' if expiry <= time.time() else status


def selected_source(provider):
    source = provider.get('credential_source')
    if source is None:
        # Keep existing definitions' priority until the user changes it.
        source = 'provider' if provider.get('credential_ref') or not provider.get('account_id') else 'account'
    if source not in ('provider', 'account'):
        raise ConfigurationError('Choose provider or account as credential_source', 422)
    return source


def binding(manager, provider):
    source = selected_source(provider)
    if source == 'provider':
        if provider.get('credential_reattach_required') and not provider.get('credential_ref'):
            raise ConfigurationError('Reattach the selected credential before applying or using this provider', 409)
        return {'source': source, 'reference': provider.get('credential_ref'), 'dependencies': {}, 'projection': None}
    if not provider.get('account_id'):
        raise ConfigurationError('Select a bound account before using account credentials', 422)
    account = manager.get('account', provider['account_id'])
    if account.get('provider_id') and account['provider_id'] != provider['id']:
        raise ConfigurationError('Account belongs to another provider', 422)
    if account.get('kind') not in ('api_key', 'oauth') or not account.get('credential_ref'):
        raise ConfigurationError('Selected account has no usable credential; no other key was substituted', 409)
    projection = None
    if account['kind'] == 'oauth':
        expiry = account.get('expires_at')
        if expiry is not None and (isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry)):
            raise ConfigurationError('Official account expiry is invalid', 409)
        if account.get('status') != 'authorized' or (expiry is not None and expiry <= time.time()):
            raise ConfigurationError('Official account credentials are expired or unavailable', 409)
        projection = {'kind': 'oauth_access_token', 'expires_at': expiry}
    return {'source': source, 'reference': account['credential_ref'], 'dependencies': {account['id']: account['version']},
            'account': account, 'projection': projection}


def projected_secret(raw, projection):
    if not projection or projection.get('kind') != 'oauth_access_token':
        raise ConfigurationError('Unsupported native credential projection', 422)
    expiry = projection.get('expires_at')
    if expiry is not None and expiry <= time.time():
        raise ConfigurationError('Official account credentials expired after preview; refresh before applying', 409)
    try:
        bundle = json.loads(raw)
        value = bundle['access_token']
    except (ValueError, TypeError, KeyError):
        raise ConfigurationError('Official account token bundle is unavailable', 409) from None
    if not isinstance(value, str) or not value or len(value) > 16384 or any(char in value for char in '\x00\r\n'):
        raise ConfigurationError('Official account access token is unavailable', 409)
    return value


def resolved_key(manager, choice):
    if not choice['reference']:
        return None
    raw = manager.vault.get(choice['reference'])
    return projected_secret(raw, choice['projection']) if choice['projection'] else raw

"""Declared API surfaces; a protocol family is not endpoint compatibility."""
from app.integrations.config_transactions import ConfigurationError

ENDPOINTS = {'openai': ('chat/completions', 'responses'), 'anthropic': ('messages',), 'gemini': ('generateContent',)}
WIRE_ENDPOINT = {'chat': 'chat/completions', 'responses': 'responses'}


def validate_endpoints(provider):
    protocol = provider.get('protocol')
    wire = provider.get('wire_api')
    if wire is not None and (not isinstance(wire, str) or protocol != 'openai' or wire not in WIRE_ENDPOINT):
        raise ConfigurationError('wire_api must be chat or responses for an OpenAI provider', 422)
    declared = provider.get('supported_endpoints')
    if declared is not None:
        if not isinstance(declared, list) or not declared or len(declared) > 4 or any(not isinstance(value, str) or value not in ENDPOINTS.get(protocol, ()) for value in declared):
            raise ConfigurationError('Declare supported endpoints from the selected protocol family', 422)
        provider['supported_endpoints'] = list(dict.fromkeys(declared))
        if wire is not None and WIRE_ENDPOINT[wire] not in declared:
            raise ConfigurationError('Preferred OpenAI API is missing from supported_endpoints', 422)
        if protocol == 'openai' and wire is None and len(provider['supported_endpoints']) == 1:
            provider['wire_api'] = next(key for key, endpoint in WIRE_ENDPOINT.items() if endpoint == declared[0])
    elif wire is not None:
        provider['supported_endpoints'] = [WIRE_ENDPOINT[wire]]
    # Existing declarations remain unknown, rather than silently rewriting a
    # working multi-endpoint provider based on whichever host is viewed first.
    return provider


def declared_endpoints(provider):
    # Validate stored/imported definitions too. Merely belonging to some known
    # protocol family is insufficient (e.g. Messages on an OpenAI provider).
    value = validate_endpoints(dict(provider))
    return value.get('supported_endpoints')


def require_endpoint(provider, endpoint):
    declared = declared_endpoints(provider)
    if declared is not None and endpoint not in declared:
        raise ConfigurationError('This provider does not declare the API endpoint required by this host: ' + endpoint, 422)


def inference_endpoint(provider):
    if provider.get('protocol') == 'anthropic':
        endpoint = 'messages'
    elif provider.get('protocol') == 'openai':
        declared = declared_endpoints(provider)
        endpoint = WIRE_ENDPOINT.get(provider.get('wire_api')) or ('responses' if declared == ['responses'] else 'chat/completions')
    else:
        raise ConfigurationError('Inference checks for this protocol are not supported', 422)
    require_endpoint(provider, endpoint)
    return endpoint

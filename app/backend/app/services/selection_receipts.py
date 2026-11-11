"""Project legacy bundle selection from explicit applied receipts, without writes."""

EVENT_FILTER = """status='applied' AND (
    json_extract(payload,'$.metadata.activate_provider')=1 OR
    json_extract(payload,'$.metadata.activation_restore')=1)"""

HEADER_SQL = """json_object(
    'provider_id',json_extract(data,'$.provider_id'),
    'model_id',json_extract(data,'$.model_id'),
    'last_plan_id',json_extract(data,'$.last_plan_id'),
    'selection_receipt_version',json_extract(data,'$.selection_receipt_version'),
    'selection_receipt_unknown',json_extract(data,'$.selection_receipt_unknown'),
    'has_selection_fields',CASE WHEN json_extract(data,'$.selection_receipt_version')=1 THEN 0 ELSE
        EXISTS(SELECT 1 FROM json_each(data,'$.fields') field
            WHERE json_extract(field.value,'$.owner')='activation:'||management_activations.host_id) END)"""


def selected_state(conn, host_id, active):
    if not active or active.get('selection_receipt_version') == 1 or not active.get('last_plan_id'):
        return active
    last = conn.execute("""SELECT json_extract(payload,'$.metadata.bundle_id') bundle_id,
        json_extract(payload,'$.metadata.activate_provider') activates FROM management_plans
        WHERE id=? AND status='applied' AND json_extract(payload,'$.metadata.host_id')=?""",
        (active['last_plan_id'], host_id)).fetchone()
    if not last or not last['bundle_id'] or last['activates']:
        return active
    event = conn.execute("""SELECT json_extract(payload,'$.metadata.activation_restore') restores,
        json_extract(payload,'$.metadata.provider_id') provider_id,
        json_extract(payload,'$.metadata.model_id') model_id FROM management_plans WHERE """ + EVENT_FILTER + """
        AND json_extract(payload,'$.metadata.host_id')=? ORDER BY updated_at DESC,id DESC LIMIT 1""", (host_id,)).fetchone()
    result = {**active, 'provider_id': None, 'model_id': None}
    has_fields = active.get('has_selection_fields') or any(isinstance(field, dict) and field.get('owner') == 'activation:' + host_id for field in active.get('fields', []))
    unknown = bool(has_fields and event is None)
    if event and not event['restores']:
        if isinstance(event['provider_id'], str) and isinstance(event['model_id'], str) and event['provider_id'] and event['model_id']:
            result.update(provider_id=event['provider_id'], model_id=event['model_id'])
        else:
            unknown = True
    result['selection_receipt_unknown'] = unknown
    return result

"""Fail-closed read-after-write verification for VLESS multi-inbound fulfillment.

No writes to 3x-ui. Does not retry a potentially successful API write.
For other protocols we need protocol-specific credential checks before allowing them.
"""
import json
import uuid

from inbound_policy import select_for_server


class VerificationError(RuntimeError):
    """The outcome is unconfirmed: do not retry issuing; reconcile manually."""


def _str(value):
    return value.strip() if isinstance(value, str) else ''


def _uuid(value):
    value = _str(value)
    if not value:
        raise VerificationError('UUID клиента отсутствует')
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise VerificationError('UUID клиента некорректен') from exc


def inspect_vless_client(inbounds, inbound_ids, email, *, expected_uuid=None,
                         expected_subid=None, minimum_expiry_ms=None):
    """Confirm selected VLESS client on ALL inbounds and absent outside selection.

    Validation is deliberately strict and read-only. A mismatch needs manual
    reconciliation; don't automatically create/attach the client a second time.
    """
    if not isinstance(inbounds, list) or not inbounds:
        raise VerificationError('3x-ui не вернула список inbound')
    if not isinstance(inbound_ids, (list, tuple)) or not inbound_ids:
        raise VerificationError('Не выбраны inbound')
    try:
        wanted = [int(i) for i in inbound_ids]
    except (TypeError, ValueError) as exc:
        raise VerificationError('Некорректные inbound ID') from exc
    if (any(i <= 0 or isinstance(v, bool) for i, v in zip(wanted, inbound_ids))
            or len(set(wanted)) != len(wanted)):
        raise VerificationError('Некорректные или повторяющиеся inbound ID')
    email = _str(email)
    if not email:
        raise VerificationError('Отсутствует email клиента')

    inbound_index = {}
    for inbound in inbounds:
        if not isinstance(inbound, dict):
            raise VerificationError('Некорректный формат inbound')
        try:
            iid = int(inbound.get('id'))
        except (TypeError, ValueError) as exc:
            raise VerificationError('Inbound без корректного ID') from exc
        if iid in inbound_index:
            raise VerificationError('Повторяющиеся inbound ID в ответе')
        inbound_index[iid] = inbound

    uuid_values = []
    subid_values = []
    found_on = []
    for iid, inbound in inbound_index.items():
        raw_settings = inbound.get('settings', {})
        if isinstance(raw_settings, str):
            try:
                settings = json.loads(raw_settings)
            except (ValueError, TypeError) as exc:
                raise VerificationError('Некорректный JSON настроек inbound') from exc
        else:
            settings = raw_settings
        if not isinstance(settings, dict):
            raise VerificationError('Некорректные настройки inbound')
        clients = settings.get('clients')
        if not isinstance(clients, list):
            if iid in wanted:
                raise VerificationError('У выбранного inbound нет списка клиентов')
            continue
        matches = [c for c in clients if isinstance(c, dict) and c.get('email') == email]
        if len(matches) > 1:
            raise VerificationError('Дублирование email в inbound')
        if not matches:
            continue
        found_on.append(iid)
        if iid not in wanted:
            raise VerificationError('Клиент присутствует в постороннем или служебном inbound')
        if inbound.get('enable') is not True:
            raise VerificationError('Выбранный inbound отключён')
        if inbound.get('protocol') != 'vless':
            raise VerificationError('Проверка пока поддерживает только VLESS')
        c = matches[0]
        if c.get('enable') is not True:
            raise VerificationError('Клиент отключён')
        uuid_values.append(_uuid(c.get('id')))
        subid_values.append(_str(c.get('subId')))
        if minimum_expiry_ms is not None:
            try:
                expiry = int(c.get('expiryTime'))
            except (ValueError, TypeError) as exc:
                raise VerificationError('Срок действия клиента неизвестен') from exc
            if expiry < int(minimum_expiry_ms) - 60000:
                raise VerificationError('Срок действия клиента меньше выданного')

    if set(found_on) != set(wanted):
        raise VerificationError('Клиент найден не во всех выбранных inbound')
    if len(set(uuid_values)) != 1:
        raise VerificationError('UUID отличаются между inbound')
    if any(not s for s in subid_values) or len(set(subid_values)) != 1:
        raise VerificationError('SubID отсутствует или отличается между inbound')
    uid = uuid_values[0]
    sid = subid_values[0]
    if expected_uuid and uid != _uuid(expected_uuid):
        raise VerificationError('UUID не совпадает с ответом на создание')
    if expected_subid and sid != _str(expected_subid):
        raise VerificationError('SubID не совпадает с ответом на создание')
    return {'uuid': uid, 'subid': sid, 'inbound_ids': wanted}


def confirm_vless_client(xui, db_path, server_id, inbound_ids, email, *,
                         expected_uuid=None, expected_subid=None,
                         minimum_expiry_ms=None):
    """One READ of the panel after writing. No automated retry or modifications."""
    fresh = xui.get_inbounds(use_cache=False)
    if not fresh:
        raise VerificationError('Не удалось повторно получить inbound из 3x-ui')
    selected_now = select_for_server(db_path, server_id, fresh)
    if set(int(i) for i in selected_now) != set(int(i) for i in inbound_ids):
        raise VerificationError('Правило inbound изменилось во время выдачи')
    return inspect_vless_client(
        fresh, inbound_ids, email,
        expected_uuid=expected_uuid, expected_subid=expected_subid,
        minimum_expiry_ms=minimum_expiry_ms,
    )

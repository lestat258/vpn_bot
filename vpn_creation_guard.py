"""Guards for new-client provisioning. This module makes no API writes."""
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


class CreationPreflightError(RuntimeError):
    pass


def gb_to_bytes(value):
    """3x-ui stores totalGB as byte count (0 means unlimited)."""
    if isinstance(value, bool):
        raise CreationPreflightError('Некорректный лимит трафика')
    try:
        gb = Decimal(str(0 if value is None or value == '' else value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CreationPreflightError('Некорректный лимит трафика') from exc
    if not gb.is_finite() or gb < 0 or gb > Decimal('1000000'):
        raise CreationPreflightError('Лимит трафика вне допустимого диапазона')
    return int((gb * (1024 ** 3)).to_integral_value(rounding=ROUND_HALF_UP))


def assert_absent_on_all_inbounds(inbounds, email):
    """Refuse to create a client whose email is already used on ANY inbound."""
    if not isinstance(inbounds, list) or not inbounds:
        raise CreationPreflightError('3x-ui не вернула полный список inbound')
    if not isinstance(email, str) or not email.strip():
        raise CreationPreflightError('Email нового клиента не задан')
    seen_ids = set()
    for inbound in inbounds:
        if not isinstance(inbound, dict):
            raise CreationPreflightError('Некорректная запись inbound')
        iid = inbound.get('id')
        try:
            iid_int = int(iid)
        except (ValueError, TypeError) as exc:
            raise CreationPreflightError('Некорректный ID inbound') from exc
        if iid_int <= 0 or iid_int in seen_ids:
            raise CreationPreflightError('Повторяющийся или некорректный inbound')
        seen_ids.add(iid_int)
        settings = inbound.get('settings')
        if isinstance(settings, str):
            try:
                settings = json.loads(settings)
            except (ValueError, TypeError) as exc:
                raise CreationPreflightError('Некорректные настройки inbound') from exc
        if not isinstance(settings, dict):
            raise CreationPreflightError('Некорректные настройки inbound')
        clients = settings.get('clients')
        if not isinstance(clients, list):
            raise CreationPreflightError('Не удалось проверить клиентов во всех inbound')
        for client in clients:
            if not isinstance(client, dict):
                raise CreationPreflightError('Некорректная запись клиента')
            if client.get('email') == email:
                raise CreationPreflightError(
                    'Email уже существует в 3x-ui; нужна ручная сверка до создания'
                )
    return True

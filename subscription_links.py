"""Strict construction of a public 3x-ui subscription URL from a configured base.

Leave a server's sub_url blank to retain the existing automatic behavior.
When set, the base must be an HTTPS prefix ending with '/' or contain
exactly one '{subid}' placeholder. No network access is performed here.
"""
import re
from urllib.parse import quote, urlsplit

_SUBID_PATTERN = re.compile(r"[A-Za-z0-9_-]{3,128}\Z")


def validate_subscription_base(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Адрес подписки должен быть строкой")
    base = value.strip()
    if not base:
        raise ValueError("Адрес подписки пуст")
    if any(ch.isspace() for ch in base):
        raise ValueError("В адресе подписки недопустимы пробелы")
    try:
        parsed = urlsplit(base)
        _ = parsed.port  # reject malformed port numbers
    except ValueError as exc:
        raise ValueError("Некорректный порт или адрес подписки") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("Адрес подписки должен быть HTTPS URL с хостом")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Учётные данные в URL подписки недопустимы")
    if parsed.query or parsed.fragment or '?' in base or '#' in base:
        raise ValueError("Query и fragment в шаблоне подписки не допускаются")
    if base.count("{subid}") == 1 and base.count('{') == 1 and base.count('}') == 1:
        return base
    if '{' in base or '}' in base:
        raise ValueError("Поддерживается только один плейсхолдер {subid}")
    if not base.endswith('/'):
        raise ValueError("Адрес подписки должен оканчиваться на / или содержать {subid}")
    return base


def build_subscription_link(base: str, subid: str) -> str:
    prefix = validate_subscription_base(base)
    if not isinstance(subid, str) or not _SUBID_PATTERN.fullmatch(subid):
        raise ValueError("3x-ui вернула некорректный SubID")
    encoded = quote(subid, safe='')
    if '{subid}' in prefix:
        return prefix.replace('{subid}', encoded)
    return prefix + encoded

#!/usr/bin/env python3
"""Offline tests for server editor and auth-token adapter. No network requests."""
import ast
from pathlib import Path
from jinja2 import Environment

ROOT = Path(__file__).resolve().parent

def check_code():
    servers = ROOT / 'admin_modules/servers.py'
    xui = ROOT / 'services/xui_client.py'
    route_tree = ast.parse(servers.read_text(encoding='utf-8'))
    ast.parse(xui.read_text(encoding='utf-8'))
    names = {n.name: n for n in ast.walk(route_tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for route in ('edit_server_page', 'edit_server'):
        node = names[route]
        assert any('check_auth(' in ast.unparse(n) for n in ast.walk(node)
                   if isinstance(n, ast.Call)), route + ' must check admin auth'
    assert 'vpn_bot_edit_csrf' in ast.unparse(names['edit_server'])
    template = (ROOT/'templates/edit_server_safe.html').read_text(encoding='utf-8')
    Environment(autoescape=True).parse(template)
    assert 'type="password"' in template and '{{ csrf | e }}' in template
    print('OK: routes, authorization, CSRF and edit form')

def check_token_adapter():
    from encryption import encrypt
    from services.xui_client import XUIClient
    plain = 'vpn4us-offline-dummy-token'
    encrypted = encrypt(plain)
    for label, token in [('legacy', plain), ('encrypted', encrypted)]:
        client = XUIClient('https://example.invalid', token)
        assert client.session.headers['Authorization'] == 'Bearer ' + plain, label
        assert client.session.verify is True, label
        client.session.close()
        print('OK:', label, 'API token and TLS verification')

if __name__ == '__main__':
    check_code()
    check_token_adapter()
    print('All offline stage3 tests passed; no external APIs contacted')

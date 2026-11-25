"""Static Telegram UI wiring checks; imports no bot modules and contacts no services.

Guarantees that code-defined reply/inline buttons have registered handlers, and
public /commands aren't advertised without a handler. Dynamic callback prefixes
are checked against registered prefix handlers.
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HANDLERS = ('handlers/users.py', 'handlers/profile.py',
            'handlers/payments.py', 'handlers/admin.py')
BUTTON_FILES = ('keyboards/keyboards.py',) + HANDLERS


def source_tree(path):
    return ast.parse((ROOT / path).read_text(encoding='utf-8'), filename=path)


def call_name(expr):
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


def text_const(expr):
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    return None


def button_callback(expr):
    value = text_const(expr)
    if value is not None:
        return ('exact', value)
    if isinstance(expr, ast.JoinedStr):
        pieces = expr.values
        if pieces and isinstance(pieces[0], ast.Constant) and isinstance(pieces[0].value, str):
            if pieces[0].value:
                return ('prefix', pieces[0].value)
    return None


def handler_rules(kind):
    exact, prefix = set(), set()
    for path in HANDLERS:
        tree = source_tree(path)
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                if call_name(decorator.func) != kind:
                    continue
                for part in ast.walk(decorator):
                    if isinstance(part, ast.Compare) and isinstance(part.ops[0], ast.Eq):
                        value = text_const(part.comparators[0])
                        if value is not None and isinstance(part.left, ast.Attribute):
                            if (kind == 'callback_query' and part.left.attr == 'data') or (
                                kind == 'message' and part.left.attr == 'text'
                            ):
                                exact.add(value)
                    if isinstance(part, ast.Call) and call_name(part.func) == 'startswith':
                        if part.args and text_const(part.args[0]) is not None:
                            prefix.add(text_const(part.args[0]))
    return exact, prefix


def callback_buttons():
    output = []
    for path in BUTTON_FILES:
        tree = source_tree(path)
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            name = call_name(call.func)
            values = []
            if name == 'InlineKeyboardButton':
                values = [kw.value for kw in call.keywords if kw.arg == 'callback_data']
            elif name == 'get_back_keyboard':
                values = call.args[:1]
            elif name == 'get_confirmation_keyboard':
                values = call.args[:2]
            for value in values:
                label = button_callback(value)
                if label is not None:
                    output.append((path, getattr(call, 'lineno', '?'), label))
    return output


def commands():
    tree = source_tree('bot.py')
    result = set()
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and call_name(call.func) == 'BotCommand':
            for kw in call.keywords:
                if kw.arg == 'command':
                    value = text_const(kw.value)
                    if value is not None:
                        result.add(value)
    return result


def handled_commands():
    result = set()
    for path in HANDLERS:
        tree = source_tree(path)
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call) or call_name(dec.func) != 'message':
                    continue
                for sub in ast.walk(dec):
                    if isinstance(sub, ast.Call) and call_name(sub.func) == 'Command':
                        for arg in sub.args:
                            value = text_const(arg)
                            if value:
                                result.add(value)
                    if isinstance(sub, ast.Call) and call_name(sub.func) == 'CommandStart':
                        result.add('start')
                    if isinstance(sub, ast.Compare) and sub.comparators:
                        value = text_const(sub.comparators[0])
                        if value and value.startswith('/') and isinstance(sub.left, ast.Attribute):
                            result.add(value[1:])
    return result


def reply_buttons():
    output = []
    tree = source_tree('keyboards/keyboards.py')
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and call_name(call.func) == 'KeyboardButton':
            for kw in call.keywords:
                if kw.arg == 'text' and text_const(kw.value):
                    output.append(text_const(kw.value))
    return output


class TelegramUiWiring(unittest.TestCase):
    def test_no_missing_callback_handlers(self):
        exact, prefix = handler_rules('callback_query')
        buttons = callback_buttons()
        self.assertGreater(len(buttons), 50, 'Buttons not discovered; audit invalid')
        missing = []
        for path, line, (kind, value) in buttons:
            found = (value in exact or any(value.startswith(p) for p in prefix)) if kind == 'exact' else (
                any(value.startswith(p) or p.startswith(value) for p in prefix)
                or any(e.startswith(value) for e in exact)
            )
            if not found:
                missing.append(f'{path}:{line} {kind} {value}')
        self.assertEqual(missing, [], 'Inline callbacks have no handlers')

    def test_reply_buttons_have_message_handlers(self):
        exact, prefix = handler_rules('message')
        buttons = reply_buttons()
        self.assertGreater(len(buttons), 4)
        self.assertEqual([x for x in buttons if x not in exact], [])

    def test_advertised_commands_have_handlers(self):
        advertised = commands()
        self.assertGreater(len(advertised), 5)
        self.assertEqual(advertised - handled_commands(), set())

    def test_2fa_enrollment_requires_authentication(self):
        source = (ROOT / 'admin_modules/auth.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        check = next(x for x in tree.body if isinstance(x, ast.FunctionDef)
                     and x.name == 'check_auth')
        constants = [x.value for x in ast.walk(check) if isinstance(x, ast.Constant)
                     and isinstance(x.value, str)]
        self.assertNotIn('/2fa/qr', constants)
        self.assertNotIn('/2fa/status', constants)
        self.assertNotIn("admin_pass = get_setting('admin_password') or 'admin'", source)

    def test_no_admin_unconditional_message_catchall(self):
        tree = source_tree('handlers/admin.py')
        dangerous = []
        for node in tree.body:
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and call_name(dec.func) == 'message' and not dec.args:
                    dangerous.append(node.name)
        self.assertEqual(dangerous, [])


if __name__ == '__main__':
    unittest.main()

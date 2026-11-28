"""Screen-scoped administrative forms with step-by-step navigation.

Only these form states are managed here. Other admin workflows retain their
existing routing until separately migrated.
"""
import secrets
from aiogram import BaseMiddleware, types
from states import AddTariff, EditTariff, EditText, AddServer, SendMessageToUser, AdminAddKey, AdminExtendKey
from aiogram.exceptions import TelegramAPIError
from form_navigation import is_current_screen, menu_navigation, FORM_SCREEN
from screen_navigation import render_screen, render_command, _key, _read

PARENTS = {s.state: "admin_tariffs" for s in (*AddTariff.__all_states__, *EditTariff.__all_states__)}
PARENTS[EditText.waiting.state] = "admin_texts"
SELECT = EditTariff.tariff_id.state
PARENTS.update({s.state: "admin_servers_menu" for s in AddServer.__all_states__})
SEND = SendMessageToUser.message_text.state
PARENTS[SEND] = "admin_users"
ISSUE_SELECT=AdminAddKey.tariff_id.state
SELECT_STATES={SELECT,ISSUE_SELECT}
CONFIRM_STATES={AdminAddKey.confirm.state,AdminExtendKey.confirm.state}
PARENTS.update({s.state:"admin_users" for s in (*AdminAddKey.__all_states__,*AdminExtendKey.__all_states__)})
STARTS = {"admin_add_tariff", "admin_edit_tariff", "admin_add_server"}


def parent_for(current, fields):
    if current.startswith("AdminExtendKey:"):
        sub=fields.get("subscription_id")
        if type(sub) is int and sub>0:
            return f"admin_key_detail_{sub}"
    if current == SEND or current.startswith("AdminAddKey:"):
        user = fields.get("user_id")
        if type(user) is int and user > 0:
            return f"user_back_to_{user}"
    return PARENTS[current]
PROMPT = "admin_form_prompt"
HISTORY = "admin_form_history"
BACK_TOKEN = "admin_form_back"
SELECT_KEYBOARD = "admin_form_select_keyboard"
CONFIRM_TOKEN = "admin_form_confirm_token"
STATES = {s.state:s for group in (AddTariff,EditTariff,EditText,AddServer,SendMessageToUser,AdminAddKey,AdminExtendKey)
          for s in group.__all_states__}
INPUT_FIELDS = {
    s.state:k for group,keys in (
        (AddTariff,("name","price","days","traffic","ip_limit")),
        (EditTariff,("tariff_id","name","price","days","traffic","ip_limit")),
        (AddServer,("name","url")),
        (AdminAddKey,("user_id","tariff_id","days")),
        (AdminExtendKey,("days",)),
    ) for s,k in zip(group.__all_states__,keys)
}


def active_keyboard(current,fields):
    parent=parent_for(current,fields)
    buttons=[]
    if fields.get(HISTORY):
        buttons.append([types.InlineKeyboardButton(text="🔙 Назад",callback_data=fields[BACK_TOKEN])])
    if current in CONFIRM_STATES:
        buttons.insert(0,[types.InlineKeyboardButton(text="✅ Подтвердить",callback_data=fields[CONFIRM_TOKEN])])
    if current in SELECT_STATES and fields.get(SELECT_KEYBOARD):
        for row in fields[SELECT_KEYBOARD]["inline_keyboard"]:
            buttons.append([types.InlineKeyboardButton(**b) for b in row
                            if b.get("callback_data")!=parent])
        buttons=[row for row in buttons if row]
    buttons.append([types.InlineKeyboardButton(text="❌ Отмена",callback_data=parent)])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


def new_back_token(fields):
    if fields.get(HISTORY):
        fields[BACK_TOKEN]="admin_form_back_"+secrets.token_hex(12)
    else:
        fields.pop(BACK_TOKEN,None)
    return fields


def snapshot(current,fields):
    return {"state":current,"data":{k:v for k,v in fields.items()
            if k not in (HISTORY,BACK_TOKEN,FORM_SCREEN,CONFIRM_TOKEN)}}


async def go_back(callback,state,current,fields):
    history=fields.get(HISTORY,[])
    if not history:
        await callback.answer("Предыдущего шага нет.",show_alert=True)
        return
    frame=history[-1]
    previous=frame["state"]
    restored={**frame["data"],HISTORY:history[:-1]}
    field=INPUT_FIELDS.get(previous)
    prompt=restored[PROMPT]
    if previous not in SELECT_STATES and field and field in fields:
        restored[field]=fields[field]
        value=str(fields[field])[:500]
        prompt+=f"\n\nРанее введено: {value}\nВведите значение заново."
    new_back_token(restored)
    await render_screen(callback.message,prompt,reply_markup=active_keyboard(previous,restored))
    restored[FORM_SCREEN]=tracked_id(callback.message,command=False)
    await state.set_data(restored)
    await state.set_state(STATES[previous])
    await callback.answer()



def back_keyboard(parent):
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔙 Назад", callback_data=parent)]
    ])


def callback_is_current(event):
    message = event.message
    if not is_current_screen(message, getattr(message, "message_id", None)):
        return False
    markup = getattr(message, "reply_markup", None)
    return any(getattr(button, "callback_data", None) == event.data
               for row in (getattr(markup, "inline_keyboard", None) or [])
               for button in row)


def private_admin(event, admin_id):
    message = getattr(event, "message", event)
    chat = getattr(message, "chat", None)
    return (getattr(getattr(event, "from_user", None), "id", None) == admin_id
            and getattr(chat, "type", None) == "private"
            and getattr(chat, "id", None) == admin_id)


def tracked_id(message, *, command):
    key = _key(message, command=command)
    row = _read(key) if key else None
    if not row:
        raise RuntimeError("Administrative form has no active screen")
    return row[0]


async def begin(callback, state, target, text, *, fields=None, keyboard=None):
    current=await state.get_state()
    old=await state.get_data()
    data={**(fields or {}),PROMPT:text,HISTORY:[]}
    if ((current==SELECT and target.state==EditTariff.name.state)
            or (current==ISSUE_SELECT and target.state==AdminAddKey.days.state)):
        data[HISTORY]=[*old.get(HISTORY,[]),snapshot(current,old)]
    if keyboard is not None:
        data[SELECT_KEYBOARD]=keyboard.model_dump(exclude_none=True)
    new_back_token(data)
    await state.clear()
    await render_screen(callback.message,text,reply_markup=active_keyboard(target.state,data))
    data[FORM_SCREEN]=tracked_id(callback.message,command=False)
    await state.set_data(data)
    await state.set_state(target)
    await callback.answer()


async def step(message, state, target, text, **fields):
    current=await state.get_state()
    old=await state.get_data()
    data={**old,**fields,PROMPT:text,HISTORY:[*old.get(HISTORY,[]),snapshot(current,old)]}
    if target.state in CONFIRM_STATES:
        data[CONFIRM_TOKEN]="admin_key_apply_"+secrets.token_hex(12)
    new_back_token(data)
    await render_command(message,text,reply_markup=active_keyboard(target.state,data))
    data[FORM_SCREEN]=tracked_id(message,command=True)
    await state.set_data(data)
    await state.set_state(target)


async def retry(message, state, error):
    current=await state.get_state()
    data=await state.get_data()
    text=error+"\n\n"+data[PROMPT]
    new_back_token(data)
    await render_command(message,text,reply_markup=active_keyboard(current,data))
    data[FORM_SCREEN]=tracked_id(message,command=True)
    await state.set_data(data)


async def finish(message, state, text, *, clean_cancel=False):
    current = await state.get_state()
    parent = parent_for(current, await state.get_data())
    # Clear before displaying result: an ambiguous Telegram response must not
    # repeat an already committed administrative operation.
    await state.clear()
    await render_command(message, text, reply_markup=back_keyboard(parent), clean_cancel=clean_cancel)


class AdminFormNavigation(BaseMiddleware):
    def __init__(self, admin_id):
        self.admin_id = admin_id

    async def __call__(self, handler, event, data):
        state = data.get("state")
        if state is None:
            return await handler(event, data)
        current = await state.get_state()
        callback = hasattr(event, "data") and hasattr(event, "message")
        if callback:
            value = event.data or ""
            select = value.startswith(("admin_edit_tariff_select_","admin_tariff_select_"))
            start = value in STARTS or value.startswith(("edittext_", "user_send_message_","user_add_key_","admin_extend_key_"))
            if current not in PARENTS:
                if value.startswith(("admin_form_back_","admin_key_apply_")):
                    await event.answer("Форма уже закрыта. Откройте её заново.",show_alert=True)
                    return
                if start or select:
                    if (current is not None or select or not private_admin(event, self.admin_id)
                            or not callback_is_current(event)):
                        await event.answer("Откройте актуальный раздел админки.", show_alert=True)
                        return
                return await handler(event, data)
            stored = (await state.get_data()).get(FORM_SCREEN)
            if (not private_admin(event, self.admin_id)
                    or getattr(event.message, "message_id", None) != stored
                    or not callback_is_current(event)):
                await event.answer("Откройте актуальную форму.", show_alert=True)
                return
            fields=await state.get_data()
            if value.startswith("admin_form_back_"):
                if value!=fields.get(BACK_TOKEN):
                    await event.answer("Кнопка устарела.",show_alert=True)
                    return
                await go_back(event,state,current,fields)
                return
            if value == parent_for(current, fields):
                await state.clear()
                data["raw_state"] = None
                return await handler(event, data)
            if ((current==SELECT and value.startswith("admin_edit_tariff_select_"))
                    or (current==ISSUE_SELECT and value.startswith("admin_tariff_select_"))
                    or (current in CONFIRM_STATES and value==fields.get(CONFIRM_TOKEN))):
                return await handler(event, data)
            await event.answer("Завершите ввод или нажмите «Отмена».", show_alert=True)
            return
        if current not in PARENTS:
            return await handler(event, data)
        if not private_admin(event, self.admin_id):
            await state.clear()
            return
        text = getattr(event, "text", None)
        if menu_navigation(text):
            await state.clear()
            data["raw_state"] = None
            return await handler(event, data)
        stored = (await state.get_data()).get(FORM_SCREEN)
        if not is_current_screen(event, stored, command=True):
            await state.clear()
            await event.answer("Форма устарела. Откройте её заново.")
            return
        if text == "/cancel":
            await finish(event, state, "Ввод отменён. Изменения не сохранены.", clean_cancel=True)
            return
        if current in SELECT_STATES or current in CONFIRM_STATES:
            await event.answer("Используйте кнопки формы или отправьте /cancel.")
            return
        if not isinstance(text, str) or not text.strip():
            await retry(event, state, "❌ Отправьте текст.")
            return
        if text.startswith("/") and not (text == "/skip" and (current.startswith("EditTariff:") or current==AdminAddKey.days.state)):
            await retry(event, state, "❌ Команда не относится к этой форме.")
            return
        return await handler(event, data)

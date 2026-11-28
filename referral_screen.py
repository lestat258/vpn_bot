"""Read-only Telegram screens for personal referral achievements."""
from decimal import Decimal
from html import escape
import sqlite3
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from referral_accrual import snapshot


def days(n):
    return format(Decimal(n)/100,".2f").rstrip("0").rstrip(".").replace(".",",")


def keyboard(*buttons):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label,callback_data=action)] for label,action in buttons])


def screen(path,user_id,username,page):
    back=("🔙 Назад","referral_main")
    try:
        s=snapshot(path,user_id)
    except (sqlite3.Error,ValueError,TypeError):
        return "⚠️ Реферальная статистика временно недоступна.",keyboard(("🔙 Назад","back_to_main"))
    threshold,name,rate=s["current"]
    if page=="levels":
        lines=["🏆 <b>Уровни и условия</b>",""]
        for i,(target,label,percent) in enumerate(s["levels"],1):
            mark="✅" if target<=s["paid"] else "🔒"
            suffix=" — ваш уровень" if target==threshold else ""
            lines.append(f"{mark} {i}. <b>{escape(label)}</b>{suffix}\nОт {target} платящих друзей · {percent}%")
        lines.extend(["","Учитываются лично приглашённые друзья и подтверждённые платные покупки.",
                      "Каждая платная покупка и продление приносит бонусные дни. "
                      "Процент определяется на момент начисления, включая нового платящего друга.",
                      "Регистрация, пробный период, пополнение баланса и бесплатные продления сами по себе не дают бонусов.",
                      "Дни не сгорают. Возвраты исключаются из подтверждённых начислений.",
                      "Исторические покупки до запуска программы не начисляются."])
        return "\n".join(lines),keyboard(back)
    if page=="history":
        lines=["📊 <b>История начислений</b>","Последние 10 операций:"]
        for date,action,duration,percent,units,valid in s["history"]:
            label="Продление" if action=="renew" else "Покупка"
            state=f"+{days(units)} дн." if valid else "На сверке"
            lines.append(f"\n{escape(str(date)[:10])} · {label} на {duration} дн.\n{percent}% · <b>{state}</b>")
        if not s["history"]:
            lines.append("\nНачислений пока нет.")
        return "\n".join(lines),keyboard(back)
    if page=="spend":
        return (f"🎁 <b>Бонусные дни</b>\n\nПодтверждено: {days(s['centidays'])} дн.\n"
                "Использование бонусных дней пока не активировано."),keyboard(back)
    lines=["🎁 <b>Реферальная программа</b>","",
           f"🏅 Ваш уровень: <b>{escape(name)}</b>",
           f"Вознаграждение: <b>{rate}%</b>","",
           f"👥 Приглашено друзей: {s['invited']}",
           f"💳 Платящие рефералы: {s['paid']}",
           f"🎁 Подтверждено бонусов: {days(s['centidays'])} дн.",
           f"📅 Начислено за месяц: {days(s['month_centidays'])} дн.",""]
    if s["next"]:
        target,label,percent=s["next"]
        filled=min(10,s["paid"]*10//target)
        lines.extend([f"Следующий уровень: <b>{escape(label)}</b> ({percent}%)",
                      "▓"*filled+"░"*(10-filled)+f" {s['paid']}/{target}",
                      f"Ещё {target-s['paid']} платящих друзей до повышения."])
    else:
        lines.append("🏆 Достигнут максимальный уровень!")
    lines.extend(["","🔗 Ваша ссылка:",f"<code>https://t.me/{escape(username)}?start=ref_{user_id}</code>"])
    if not s["enabled"]:
        lines.append("\n⚠️ Начисление пока не активировано.")
    return "\n".join(lines),keyboard(("🏆 Все уровни и условия","referral_levels"),
        ("🎁 Бонусные дни","referral_spend"),("📊 История начислений","referral_history"),
        ("🔙 Назад","back_to_main"))

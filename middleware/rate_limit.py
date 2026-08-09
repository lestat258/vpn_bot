"""
Middleware для ограничения запросов в боте
"""
import logging
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, Update
from rate_limiter import rate_limiter

logger = logging.getLogger(__name__)

class RateLimitMiddleware(BaseMiddleware):
    """Middleware для проверки лимитов запросов"""
    
    async def __call__(
        self,
        handler: Callable[[Update, Dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: Dict[str, Any]
    ) -> Any:
        # Определяем тип действия и ID пользователя
        user_id = None
        action_type = 'message'
        
        if event.message:
            user_id = event.message.from_user.id
            if event.message.text and event.message.text.startswith('/'):
                action_type = 'command'
            else:
                action_type = 'message'
        
        elif event.callback_query:
            user_id = event.callback_query.from_user.id
            action_type = 'callback'
            if event.callback_query.data and 'deposit' in event.callback_query.data:
                action_type = 'payment'
        
        elif event.inline_query:
            user_id = event.inline_query.from_user.id
            action_type = 'callback'
        
        if not user_id:
            return await handler(event, data)
        
        is_allowed, error_message = rate_limiter.is_allowed(user_id, action_type)
        
        if not is_allowed:
            if event.message:
                await event.message.answer(error_message, parse_mode="HTML")
            elif event.callback_query:
                await event.callback_query.answer(error_message, show_alert=True)
            return None
        
        return await handler(event, data)

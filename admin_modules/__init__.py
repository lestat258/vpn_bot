"""Модули админ-панели VPN Bot"""

from .auth import auth_router
from .dashboard import dashboard_router
from .users import users_router
from .servers import servers_router
from .tariffs import tariffs_router
from .payments import payments_router
from .promocodes import promocodes_router
from .broadcast import broadcast_router
from .analytics import analytics_router
from .backup import backup_router
from .settings import settings_router
from .security import security_router
from .rate_limits import rate_limits_router

__all__ = [
    'auth_router',
    'dashboard_router',
    'users_router',
    'servers_router',
    'tariffs_router',
    'payments_router',
    'promocodes_router',
    'broadcast_router',
    'analytics_router',
    'backup_router',
    'settings_router',
    'security_router',
    'rate_limits_router',
]

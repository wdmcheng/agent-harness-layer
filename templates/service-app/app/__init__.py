"""service-app 模板的应用入口包。"""

from app.main import create_app as create_app

_APP_FACTORY_EXPORTS = ["create_app"]

__all__ = [*_APP_FACTORY_EXPORTS]  # pyright: ignore[reportUnsupportedDunderAll]

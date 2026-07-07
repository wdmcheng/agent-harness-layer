"""API route 模块入口。"""

from app.api.routes.runs import router as _runs_router

_ROUTE_EXPORTS = ["runs_router"]

runs_router = _runs_router

__all__ = [*_ROUTE_EXPORTS]  # pyright: ignore[reportUnsupportedDunderAll]

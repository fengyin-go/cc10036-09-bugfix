"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource, workflow

# workflow 的路径都带 /api/workflow 前缀，必须排在动态 /api/{module} 之前注册，
# 否则固定前缀会被资源路由当成模块名吞掉。
ROUTERS = [workflow, resource]

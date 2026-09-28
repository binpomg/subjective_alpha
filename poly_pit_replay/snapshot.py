"""稳定的包级截面构建入口。

价格点的唯一实现仍在现有 ``scripts/build_poly_snapshot.py``，这里仅作
包级导出，保持旧脚本路径与既有调用接口不变。安装本地包时，pyproject
同时安装 ``scripts`` 包。
"""

from scripts.build_poly_snapshot import (  # noqa: F401
    build_snapshot,
    select_latest_usable_point,
)

__all__ = ["build_snapshot", "select_latest_usable_point"]

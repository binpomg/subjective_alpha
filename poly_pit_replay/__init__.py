"""封闭式 Polymarket 历史截面回放工具包。

包入口只提供数据构建、契约校验和文件封存工具；不包含常驻服务、下单或
自动读取 A 股未来行情的逻辑。
"""

from .schemas import (
    SchemaValidationError,
    validate_research_input,
    validate_signal_output,
    validate_world_output,
)
from .sealing import (
    canonical_json_bytes,
    load_and_validate_json,
    seal_json_artifact,
    sha256_bytes,
    verify_seal,
)
from .snapshot import build_snapshot, select_latest_usable_point

__version__ = "0.1.0"

__all__ = [
    "SchemaValidationError",
    "build_snapshot",
    "canonical_json_bytes",
    "load_and_validate_json",
    "select_latest_usable_point",
    "seal_json_artifact",
    "sha256_bytes",
    "validate_research_input",
    "validate_signal_output",
    "validate_world_output",
    "verify_seal",
]

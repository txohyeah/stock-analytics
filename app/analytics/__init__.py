"""A股分析层（T2：并入 stock-analytics 的‘数据同步 + A股分析’一体）。

- repository: sqlite 数据访问（表名 tushare 原生）
- screen / market / account: 分析命令实现
- baolei: 财报排雷（读 sqlite fina_indicator）
- inputs / models: A股代码解析与领域模型
"""

from .errors import (
    AnalyticsError,
    DataInsufficientError,
    DatabaseConnectionError,
    ReportWriteError,
    UserInputError,
)
from .inputs import normalize_code, parse_codes_arg, parse_input_file, parse_positions_file
from .models import MissingDataContract, MissingDataItem, PositionInput, StockCode
from .repository import SqliteRepository, StockDataRepository

__all__ = [
    "AnalyticsError",
    "DataInsufficientError",
    "DatabaseConnectionError",
    "ReportWriteError",
    "UserInputError",
    "normalize_code",
    "parse_codes_arg",
    "parse_input_file",
    "parse_positions_file",
    "MissingDataContract",
    "MissingDataItem",
    "PositionInput",
    "StockCode",
    "SqliteRepository",
    "StockDataRepository",
]
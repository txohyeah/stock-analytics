"""stock-analytics A股分析层异常（从 stock-research exceptions.py 迁移，基类改名）。"""


class AnalyticsError(Exception):
    """Base exception with an agent-readable error code and exit status."""

    error_code = "INTERNAL_ERROR"
    exit_code = 5
    hint = ""

    def __init__(self, message: str, *, hint: str | None = None, payload: dict[str, object] | None = None) -> None:
        super().__init__(message)
        if hint is not None:
            self.hint = hint
        self.payload = payload or {}


class UserInputError(AnalyticsError):
    error_code = "INVALID_ARGUMENT"
    exit_code = 1


class DatabaseConnectionError(AnalyticsError):
    error_code = "DATABASE_CONNECTION_FAILED"
    exit_code = 2
    hint = "Check DB_SQLITE_PATH or --database"


class DataInsufficientError(AnalyticsError):
    error_code = "DATA_INSUFFICIENT"
    exit_code = 3


class ReportWriteError(AnalyticsError):
    error_code = "REPORT_WRITE_FAILED"
    exit_code = 4
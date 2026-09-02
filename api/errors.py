class ApiError(Exception):
    """Raised by route handlers to produce the {"error", "message"} response shape from CLAUDE.md."""

    def __init__(self, status_code: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error
        self.message = message

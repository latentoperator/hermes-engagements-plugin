"""Errors exposed by the engagements core."""


class EngagementsError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = {} if details is None else details


class LockTimeout(EngagementsError):
    def __init__(self, path):
        super().__init__("lock_timeout", "Lock acquisition timed out.", {"path": str(path)})

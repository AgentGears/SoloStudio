class SoloStudioError(Exception):
    code = "SOLOSTUDIO_ERROR"


class InvalidCanonicalValue(SoloStudioError):
    code = "INVALID_CANONICAL_VALUE"


class InvalidCommand(SoloStudioError):
    code = "INVALID_COMMAND"


class NotFound(SoloStudioError):
    code = "NOT_FOUND"

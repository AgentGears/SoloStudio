class SoloStudioError(Exception):
    code = "SOLOSTUDIO_ERROR"
class InvalidCanonicalValue(SoloStudioError):
    code = "INVALID_CANONICAL_VALUE"
class InvalidCommand(SoloStudioError):
    code = "INVALID_COMMAND"
class NotFound(SoloStudioError):
    code = "NOT_FOUND"
class InvalidArtifact(SoloStudioError):
    code = "INVALID_ARTIFACT"
class MissingRetainedArtifact(SoloStudioError):
    code = "MISSING_RETAINED_ARTIFACT"
class ArtifactDigestMismatch(SoloStudioError):
    code = "ARTIFACT_DIGEST_MISMATCH"
class ArtifactDependencyCycle(SoloStudioError):
    code = "ARTIFACT_DEPENDENCY_CYCLE"
class BackupClosureBroken(SoloStudioError):
    code = "BACKUP_CLOSURE_BROKEN"
class BackupVerificationFailed(SoloStudioError):
    code = "BACKUP_VERIFICATION_FAILED"

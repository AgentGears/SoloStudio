# Local media runtime

M0 Slice 8 uses a local `ffmpeg`/`ffprobe` runtime for deterministic media-fixture rendering and validation.

The executables must be available on `PATH` before `media.render` execution. They are local runtime dependencies, not bundled services and not remote providers.

The kernel treats missing or unusable media tools as ArtifactJob failure; it never treats process exit alone as successful media production. Successful rendered-video registration requires a decodable MP4 with both video and audio streams plus exact variant dimension and duration checks.

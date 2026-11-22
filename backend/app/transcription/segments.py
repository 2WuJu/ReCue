"""Validation shared by local transcription engines and transcript publication."""

class TranscriptionError(ValueError):
    pass


class UnsupportedModel(TranscriptionError):
    pass


def validate_segments(segments, duration_ms):
    if type(duration_ms) is not int or duration_ms <= 0:
        raise TranscriptionError("Media duration must be a positive integer in milliseconds")
    if not isinstance(segments, list):
        raise TranscriptionError("Transcript segments must be an array")
    previous_start = -1
    for segment in segments:
        if not isinstance(segment, dict):
            raise TranscriptionError("Invalid transcript segment")
        start, end, text = (segment.get(key) for key in ("start_ms", "end_ms", "text"))
        if (type(start) is not int or type(end) is not int
                or not 0 <= start < end <= duration_ms or start < previous_start):
            raise TranscriptionError("Invalid or out-of-order timestamps; transcript not published")
        if not isinstance(text, str) or not text.strip():
            raise TranscriptionError("Transcript segment text must not be empty")
        previous_start = start

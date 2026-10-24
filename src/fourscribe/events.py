"""Wire event builders for the 40zen realtime protocol (subset actually used).

Delta carries the FULL current hypothesis of the segment (deliberate contract
choice; the client swaps fragments as provisional revisions). audio_processed
is in current-commit-epoch coordinates. event_id is unique per WS connection
and never reset per commit (client dedupe set is only cleared on a new socket).
"""
import uuid


def new_event_id() -> str:
    return uuid.uuid4().hex


def session_updated(session: dict, applied: dict, error: dict = None) -> dict:
    ev = {"type": "session.updated", "event_id": new_event_id(),
          "session": session, "applied": applied}
    if error:
        ev["error"] = error
    return ev


def delta_event(text: str, audio_processed: float) -> dict:
    return {"type": "conversation.item.input_audio_transcription.delta",
            "event_id": new_event_id(),
            "delta": text,
            "audio_processed": round(audio_processed, 3)}


def final_event(text: str, audio_processed: float, words: list,
                alignment: dict) -> dict:
    return {"type": "conversation.item.input_audio_transcription.completed",
            "event_id": new_event_id(),
            "segment_id": new_event_id(),
            "transcript": text,
            "audio_processed": round(audio_processed, 3),
            "words": words or [],
            "alignment": alignment}


def committed_event(audio_processed: float) -> dict:
    return {"type": "input_audio_buffer.committed", "event_id": new_event_id(),
            "audio_processed": round(audio_processed, 3)}


def error_event(code: str, message: str) -> dict:
    return {"type": "error", "event_id": new_event_id(),
            "code": code, "message": message}


def enhanced_event(segment_id, enhanced_text, enhancer_error, enhancer_ms,
                   status, error_code=None):
    ev = {"type": "conversation.item.input_audio_transcription.enhanced",
          "event_id": new_event_id(), "segment_id": segment_id,
          "enhanced_text": enhanced_text, "enhancer_error": enhancer_error,
          "enhancer_ms": enhancer_ms, "status": status}
    if error_code:
        ev["error"] = {"code": error_code}
    return ev

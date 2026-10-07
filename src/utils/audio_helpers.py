"""
Audio format helpers and stream serializers for Vobiz and telephony streaming.
"""

import base64
import json
from typing import Optional
from pipecat.frames.frames import (
    AudioRawFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
    EndFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer


class VobizTelephonySerializer(FrameSerializer):
    """
    Serializer for Vobiz / Media Stream WebSocket protocols.
    Supports JSON event packets:
      - 'start' / 'connected': call metadata
      - 'media': base64 audio payload (ulaw or pcm)
      - 'stop': call end
      - Raw binary audio fallback
    """

    def __init__(self, stream_sid: str = ""):
        super().__init__()
        self._stream_sid = stream_sid

    def set_stream_sid(self, stream_sid: str):
        self._stream_sid = stream_sid

    async def serialize(self, frame: Frame) -> str | bytes | None:
        """Serializes outgoing Pipecat audio frames to Vobiz media JSON."""
        if isinstance(frame, InterruptionFrame):
            return json.dumps({
                "event": "clear",
                "streamSid": self._stream_sid,
            })

        if isinstance(frame, OutputAudioRawFrame):
            payload = base64.b64encode(frame.audio).decode("utf-8")
            return json.dumps({
                "event": "media",
                "streamSid": self._stream_sid,
                "media": {
                    "payload": payload,
                },
            })

        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        """Deserializes incoming data from Vobiz to Pipecat audio frames."""
        if isinstance(data, bytes):
            # Raw binary PCM frame (16kHz / 8kHz mono)
            return InputAudioRawFrame(
                audio=data,
                num_channels=1,
                sample_rate=8000,
            )

        try:
            message = json.loads(data)
            event = message.get("event")

            if event == "media":
                media_info = message.get("media", {})
                payload_b64 = media_info.get("payload", "")
                if payload_b64:
                    raw_audio = base64.b64decode(payload_b64)
                    return InputAudioRawFrame(
                        audio=raw_audio,
                        num_channels=1,
                        sample_rate=8000,
                    )

            elif event in ("stop", "close"):
                return EndFrame()

        except Exception:
            pass

        return None

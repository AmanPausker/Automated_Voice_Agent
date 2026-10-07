"""
Audio format helpers and stream serializers for Vobiz and telephony streaming.
Supports standard G.711 μ-law, 8kHz/16kHz audio conversion, and WebSocket media stream events.
"""

import base64
import json
from typing import Optional
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    EndFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer
from pipecat.serializers.twilio import ulaw_to_pcm, pcm_to_ulaw


class VobizTelephonySerializer(FrameSerializer):
    """
    Serializer for Vobiz / Media Stream WebSocket protocols.
    Handles:
      - 'start': extracts streamSid and callSid
      - 'media': converts 8kHz μ-law audio payload to 16kHz PCM for Deepgram
      - Outgoing audio: converts 16kHz PCM from Cartesia to 8kHz μ-law for Vobiz
      - 'clear': audio interruption / barge-in clearance
      - 'stop': hang up
    """

    def __init__(self, stream_sid: str = "", sample_rate: int = 16000):
        super().__init__()
        self._stream_sid = stream_sid
        self._sample_rate = sample_rate
        self._telephony_sample_rate = 8000
        self._input_resampler = None
        self._output_resampler = None

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
            # Convert 16kHz PCM to 8kHz μ-law for telephony
            try:
                ulaw_data = await pcm_to_ulaw(
                    frame.audio,
                    self._sample_rate,
                    self._telephony_sample_rate,
                    self._output_resampler,
                )
                payload = base64.b64encode(ulaw_data).decode("utf-8")
                return json.dumps({
                    "event": "media",
                    "streamSid": self._stream_sid,
                    "media": {
                        "payload": payload,
                    },
                })
            except Exception:
                # Fallback to direct raw payload if conversion fails
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
            return InputAudioRawFrame(
                audio=data,
                num_channels=1,
                sample_rate=self._sample_rate,
            )

        try:
            message = json.loads(data)
            event = message.get("event")

            # Capture streamSid when the call starts
            if event == "start":
                start_data = message.get("start", {})
                self._stream_sid = start_data.get("streamSid") or message.get("streamSid", self._stream_sid)
                return None

            if event == "media":
                if not self._stream_sid:
                    self._stream_sid = message.get("streamSid", "")

                media_info = message.get("media", {})
                payload_b64 = media_info.get("payload", "")
                if payload_b64:
                    raw_ulaw = base64.b64decode(payload_b64)
                    # Convert 8kHz μ-law to 16kHz linear PCM
                    pcm_audio = await ulaw_to_pcm(
                        raw_ulaw,
                        self._telephony_sample_rate,
                        self._sample_rate,
                        self._input_resampler,
                    )
                    if pcm_audio:
                        return InputAudioRawFrame(
                            audio=pcm_audio,
                            num_channels=1,
                            sample_rate=self._sample_rate,
                        )

            elif event in ("stop", "close"):
                return EndFrame()

        except Exception:
            pass

        return None

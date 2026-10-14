"""
Latency-masking filler audio processor for Automated Voice Agent.
Plays pre-recorded, instant PCM audio clips ("hmm, let me check...", "one moment...")
when Time-To-First-Token (TTFT) exceeds a threshold or during asynchronous tool calls.
"""

import os
import random
import asyncio
from typing import Dict, List, Optional
from loguru import logger

from pipecat.frames.frames import (
    Frame,
    TTSAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
    InterruptionFrame,
    StartFrame,
    CancelFrame,
    EndFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection


class FillerBank:
    """In-memory bank of pre-recorded 16kHz PCM audio clips."""

    def __init__(self, fillers_dir: str = "assets/fillers"):
        self.fillers_dir = fillers_dir
        self.clips: Dict[str, List[bytes]] = {
            "thinking": [],
            "slots": [],
            "booking": [],
            "clinic": [],
        }
        self.load_clips()

    def load_clips(self):
        if not os.path.exists(self.fillers_dir):
            logger.warning(f"Fillers directory '{self.fillers_dir}' does not exist.")
            return

        for fname in sorted(os.listdir(self.fillers_dir)):
            if not fname.endswith(".pcm"):
                continue
            fpath = os.path.join(self.fillers_dir, fname)
            try:
                with open(fpath, "rb") as f:
                    pcm_data = f.read()
                
                # Classify into category
                for cat in self.clips.keys():
                    if fname.startswith(cat):
                        self.clips[cat].append(pcm_data)
                        break
                else:
                    self.clips["thinking"].append(pcm_data)

            except Exception as e:
                logger.error(f"Error loading filler clip {fname}: {e}")

        counts = {k: len(v) for k, v in self.clips.items()}
        logger.info(f"Loaded filler audio clips: {counts}")

    def get_random_clip(self, category: str = "thinking") -> Optional[bytes]:
        pool = self.clips.get(category) or self.clips.get("thinking") or []
        if not pool:
            return None
        return random.choice(pool)


class FillerProcessor(FrameProcessor):
    """
    Pipeline processor placed right after TTS and before Transport Output.
    Monitors speech turn latency and injects zero-latency pre-recorded PCM audio.
    """

    def __init__(
        self,
        filler_bank: Optional[FillerBank] = None,
        latency_threshold_secs: float = 0.75,
        sample_rate: int = 16000,
        chunk_size: int = 1600,  # 50ms chunks at 16kHz 16-bit mono
    ):
        super().__init__()
        self.bank = filler_bank or FillerBank()
        self.latency_threshold = latency_threshold_secs
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size

        self._timer_task: Optional[asyncio.Task] = None
        self._playback_task: Optional[asyncio.Task] = None
        self._is_playing_filler = False
        self._received_tts_response = False
        self._user_is_speaking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            return

        # 1. User started speaking / Interruption: immediately cancel filler
        if isinstance(frame, (VADUserStartedSpeakingFrame, InterruptionFrame)):
            self._user_is_speaking = True
            self._cancel_timer()
            self._stop_filler_playback()
            await self.push_frame(frame, direction)
            return

        # 2. User stopped speaking: start latency-masking countdown
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._user_is_speaking = False
            self._received_tts_response = False
            self._start_latency_timer()
            await self.push_frame(frame, direction)
            return

        # 3. Main TTS started pushing audio frames: cancel filler timer
        if isinstance(frame, TTSAudioRawFrame):
            self._received_tts_response = True
            self._cancel_timer()
            # If filler is still playing, stop it cleanly so main LLM audio takes over
            if self._is_playing_filler:
                self._stop_filler_playback()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, (CancelFrame, EndFrame)):
            self._cancel_timer()
            self._stop_filler_playback()
            await self.push_frame(frame, direction)
            return

        # Forward other frames downstream
        await self.push_frame(frame, direction)

    def _start_latency_timer(self):
        self._cancel_timer()
        self._timer_task = asyncio.create_task(self._latency_timer_worker())

    def _cancel_timer(self):
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = None

    async def _latency_timer_worker(self):
        try:
            await asyncio.sleep(self.latency_threshold)
            # If threshold elapsed and no TTS audio or speech happened, trigger thinking filler
            if not self._received_tts_response and not self._user_is_speaking and not self._is_playing_filler:
                logger.debug(f"TTFT exceeded {self.latency_threshold}s, triggering thinking filler.")
                await self.trigger_filler("thinking")
        except asyncio.CancelledError:
            pass

    async def trigger_filler(self, category: str = "thinking"):
        """Plays a pre-recorded filler clip immediately."""
        if self._user_is_speaking or self._is_playing_filler:
            return

        clip = self.bank.get_random_clip(category)
        if not clip:
            return

        self._stop_filler_playback()
        self._playback_task = asyncio.create_task(self._stream_clip(clip))

    async def _stream_clip(self, clip: bytes):
        self._is_playing_filler = True
        logger.info("Playing latency-masking filler audio clip (0 ms TTS latency)")
        try:
            chunk_duration = self.chunk_size / (self.sample_rate * 2)  # 2 bytes per sample
            for i in range(0, len(clip), self.chunk_size):
                if not self._is_playing_filler or self._user_is_speaking or self._received_tts_response:
                    break

                chunk = clip[i : i + self.chunk_size]
                frame = TTSAudioRawFrame(
                    audio=chunk,
                    sample_rate=self.sample_rate,
                    num_channels=1,
                )
                await self.push_frame(frame, FrameDirection.DOWNSTREAM)
                await asyncio.sleep(chunk_duration)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error streaming filler clip: {e}")
        finally:
            self._is_playing_filler = False

    def _stop_filler_playback(self):
        self._is_playing_filler = False
        if self._playback_task and not self._playback_task.done():
            self._playback_task.cancel()
        self._playback_task = None

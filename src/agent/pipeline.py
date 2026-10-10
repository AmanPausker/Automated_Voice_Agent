"""
Pipecat voice agent pipeline implementation.
Integrates VAD, STT, LLM (with Cal.com function calling), and TTS.
"""

import os
from typing import Optional
from loguru import logger
from dotenv import load_dotenv

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import FunctionSchema, LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.frames.frames import LLMMessagesAppendFrame

from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.services.llm_service import FunctionCallParams

from src.agent.prompts import get_system_prompt
from src.tasks.cal_booking import get_available_slots, book_appointment
from src.user.user_service import get_caller_profile
from db.database import save_call_start, save_call_end, set_whatsapp_preference
from src.utils.whatsapp import (
    WHATSAPP_TENANT_ID,
    is_valid_e164_phone,
)

load_dotenv()


def create_agent_pipeline(
    transport,
    call_id: str,
    caller_phone: str = "",
):
    """
    Constructs and returns (task, runner) for a given call session.
    """
    save_call_start(call_id=call_id, caller_number=caller_phone)

    # 1. Fetch user profile for context personalization
    profile = get_caller_profile(caller_phone)
    caller_name = profile.get("name", "")
    system_prompt = get_system_prompt(caller_name=caller_name, caller_phone=caller_phone)

    # 2. VAD (Voice Activity Detection with tuned low-latency silence threshold)
    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(
                start_secs=0.08,
                stop_secs=0.25,
                confidence=0.7,
            )
        )
    )

    # 3. STT (Speech-to-Text: Deepgram)
    deepgram_key = os.getenv("DEEPGRAM_API_KEY", "").strip().strip('"')
    if not deepgram_key:
        logger.warning("DEEPGRAM_API_KEY is not set. Please add it to your .env file.")
    stt = DeepgramSTTService(
        api_key=deepgram_key,
        encoding="linear16",
        sample_rate=16000,
    )

    # 4. LLM Service (OpenRouter or OpenAI)
    openrouter_key = (os.getenv("OPEN_ROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY") or "").strip().strip('"')
    openai_key = (os.getenv("OPENAI_API_KEY") or "").strip().strip('"')
    llm_model = os.getenv("LLM_MODEL", "openai/gpt-4o-mini")

    if openrouter_key:
        logger.info(f"Using OpenRouter with model: {llm_model}")
        llm = OpenAILLMService(
            api_key=openrouter_key,
            base_url="https://openrouter.ai/api/v1",
            settings=OpenAILLMService.Settings(
                model=llm_model,
                system_instruction=system_prompt,
                max_tokens=120,
                temperature=0.7,
            ),
        )
    elif openai_key:
        logger.info("Using native OpenAI gpt-4o-mini")
        llm = OpenAILLMService(
            api_key=openai_key,
            settings=OpenAILLMService.Settings(
                model="gpt-4o-mini",
                system_instruction=system_prompt,
                max_tokens=120,
                temperature=0.7,
            ),
        )
    else:
        logger.warning("Neither OPEN_ROUTER_API_KEY nor OPENAI_API_KEY is set in .env")
        llm = OpenAILLMService(
            api_key="sk-placeholder",
            settings=OpenAILLMService.Settings(
                model="gpt-4o-mini",
                system_instruction=system_prompt,
                max_tokens=120,
                temperature=0.7,
            ),
        )

    # 5. Define Tool Schemas
    check_slots_schema = FunctionSchema(
        name="check_available_slots",
        description="Check available meeting slots on Cal.com for a specific date (YYYY-MM-DD).",
        properties={
            "date": {
                "type": "string",
                "description": "Date in YYYY-MM-DD format (e.g. 2026-10-09).",
            }
        },
        required=["date"],
    )

    book_appt_schema = FunctionSchema(
        name="book_appointment",
        description="Book a meeting slot on Cal.com for the caller once they provide details.",
        properties={
            "start_time": {
                "type": "string",
                "description": "Selected slot ISO timestamp (e.g. 2026-10-09T10:00:00Z).",
            },
            "name": {
                "type": "string",
                "description": "Full name of the caller.",
            },
            "email": {
                "type": "string",
                "description": "Email address of the caller for calendar invite.",
            },
            "notes": {
                "type": "string",
                "description": "Optional notes or purpose of the meeting.",
            },
        },
        required=["start_time", "name", "email"],
    )

    whatsapp_preferences_schema = FunctionSchema(
        name="manage_whatsapp_notifications",
        description=(
            "Record the caller's explicit choice to enable or disable appointment "
            "confirmation messages on their WhatsApp number."
        ),
        properties={
            "enabled": {
                "type": "boolean",
                "description": (
                    "True only after the caller explicitly agrees; false when they "
                    "decline or request that WhatsApp notifications be stopped."
                ),
            }
        },
        required=["enabled"],
    )

    # 6. Register Tool Handlers
    async def handle_check_slots(params: FunctionCallParams):
        date_str = params.arguments.get("date", "")
        logger.info(f"Checking available slots for: {date_str}")
        slots = await get_available_slots(date_str)
        if slots:
            # Return first 4 available slots to avoid overwhelming the voice agent
            available = slots[:4]
            await params.result_callback({
                "status": "success",
                "date": date_str,
                "available_slots": available,
                "count": len(slots),
            })
        else:
            await params.result_callback({
                "status": "none_available",
                "message": f"No available slots found for {date_str}. Please ask the user for another date.",
            })

    async def handle_book_appt(params: FunctionCallParams):
        start_time = params.arguments.get("start_time", "")
        name = params.arguments.get("name", "")
        email = params.arguments.get("email", "")
        notes = params.arguments.get("notes", "")

        logger.info(f"Booking appointment for {name} ({email}) at {start_time}")
        result = await book_appointment(
            start_time=start_time,
            name=name,
            email=email,
            phone=caller_phone,
            notes=notes,
            call_id=call_id,
        )
        await params.result_callback(result)

    async def handle_whatsapp_preferences(params: FunctionCallParams):
        enabled = params.arguments.get("enabled")
        if not isinstance(enabled, bool):
            await params.result_callback(
                {"success": False, "message": "A clear enable or disable choice is required."}
            )
            return
        if not is_valid_e164_phone(caller_phone):
            await params.result_callback(
                {
                    "success": False,
                    "message": (
                        "The caller ID is not a valid international phone number. "
                        "Do not enable WhatsApp notifications."
                    ),
                }
            )
            return
        set_whatsapp_preference(
            tenant_id=WHATSAPP_TENANT_ID,
            phone_number=caller_phone,
            enabled=enabled,
            consent_source="voice_call",
            consent_text_version="whatsapp-consent-v1",
        )
        await params.result_callback(
            {
                "success": True,
                "enabled": enabled,
                "message": (
                    "WhatsApp notifications are enabled."
                    if enabled
                    else "WhatsApp notifications are disabled."
                ),
            }
        )

    llm.register_function("check_available_slots", handle_check_slots)
    llm.register_function("book_appointment", handle_book_appt)
    llm.register_function("manage_whatsapp_notifications", handle_whatsapp_preferences)

    # 7. TTS (Text-to-Speech: Cartesia or OpenAI)
    cartesia_key = os.getenv("CARTESIA_API_KEY", "").strip().strip('"')
    if cartesia_key:
        voice_id = os.getenv("CARTESIA_VOICE_ID", "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc")
        tts = CartesiaTTSService(
            api_key=cartesia_key,
            settings=CartesiaTTSService.Settings(voice=voice_id),
            sample_rate=16000,
        )
    else:
        logger.info("Using OpenAITTSService (fallback since CARTESIA_API_KEY is not set)")
        tts = OpenAITTSService(
            api_key=openai_key or "sk-placeholder",
            voice="alloy",
            sample_rate=16000,
        )

    # 8. Conversation Context & Aggregators (Tuned for ultra-low 350ms turn latency)
    context = LLMContext(
        tools=[check_slots_schema, book_appt_schema, whatsapp_preferences_schema],
    )
    user_params = LLMUserAggregatorParams(
        user_turn_strategies=UserTurnStrategies(
            stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.35, wait_for_transcript=True)]
        )
    )
    context_aggregator = LLMContextAggregatorPair(context, user_params=user_params)

    # 9. Assembly
    pipeline = Pipeline([
        transport.input(),
        vad,
        stt,
        context_aggregator.user(),
        llm,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=True),
    )

    # Initial greeting trigger
    async def trigger_greeting():
        greeting_prompt = (
            f"Greet returning caller {caller_name} warmly and ask how you can help."
            if caller_name
            else "Greet the caller warmly and ask how you can assist them today."
        )
        await task.queue_frames([
            LLMMessagesAppendFrame(
                messages=[{"role": "system", "content": greeting_prompt}],
                run_llm=True,
            )
        ])

    runner = PipelineRunner()

    return task, runner, trigger_greeting

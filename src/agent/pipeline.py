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
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.services.llm_service import FunctionCallParams

from src.agent.prompts import get_system_prompt
from src.agent.filler_processor import FillerProcessor, FillerBank
from src.tasks.cal_booking import get_available_slots as cal_get_available_slots, book_appointment as cal_book_appointment
from src.tasks.google_calendar import (
    get_available_slots_google,
    book_appointment_google,
    cancel_appointment_google,
    reschedule_appointment_google,
)
from src.user.user_service import get_caller_profile
from db.database import (
    save_call_start,
    save_call_end,
    set_whatsapp_preference,
    get_clinic_config,
    list_doctors,
)
from src.utils.whatsapp import (
    WHATSAPP_TENANT_ID,
    WHATSAPP_HOSPITAL_NAME,
    WHATSAPP_HOSPITAL_ADDRESS,
    WHATSAPP_HOSPITAL_PHONE,
    WHATSAPP_TIMEZONE,
    is_valid_e164_phone,
)

load_dotenv()

_shared_filler_bank = FillerBank()

SCHEDULING_PROVIDER = os.getenv("SCHEDULING_PROVIDER", "calcom").lower()


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

    # 3. STT (Speech-to-Text: Sarvam Saaras or Deepgram Nova-2)
    stt_provider = os.getenv("STT_PROVIDER", "sarvam").strip().lower()
    sarvam_key = os.getenv("SARVAM_API_KEY", "").strip().strip('"')
    deepgram_key = os.getenv("DEEPGRAM_API_KEY", "").strip().strip('"')

    if stt_provider == "sarvam" and sarvam_key:
        logger.info("Using SarvamSTTService (Saaras)")
        stt = SarvamSTTService(
            api_key=sarvam_key,
        )
    else:
        logger.info("Using DeepgramSTTService (Nova-2)")
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

    doctor_name = os.getenv("DOCTOR_NAME", "Dr. Patil").strip()
    clinic_name = os.getenv("CLINIC_NAME", f"{doctor_name}'s Clinic").strip()

    # 5. Define Tool Schemas
    clinic_timings_schema = FunctionSchema(
        name="get_clinic_timings",
        description=f"Get official clinic working hours, break windows, working days, holidays, and address for {clinic_name}.",
        properties={},
        required=[],
    )

    check_slots_schema = FunctionSchema(
        name="check_available_slots",
        description=f"Check available appointment slots with {doctor_name} for a specific date (YYYY-MM-DD).",
        properties={
            "date": {
                "type": "string",
                "description": "Date in YYYY-MM-DD format (e.g. 2026-10-15).",
            },
        },
        required=["date"],
    )

    book_appt_schema = FunctionSchema(
        name="book_appointment",
        description=f"Confirm and book an appointment with {doctor_name}, date/time slot, and patient details.",
        properties={
            "slot_start": {
                "type": "string",
                "description": "Selected slot ISO timestamp (e.g. 2026-10-15T10:00:00Z).",
            },
            "name": {
                "type": "string",
                "description": "Full name of the patient.",
            },
            "reason": {
                "type": "string",
                "description": "Brief reason for consultation (optional).",
            },
        },
        required=["slot_start", "name"],
    )

    cancel_appt_schema = FunctionSchema(
        name="cancel_appointment",
        description="Cancel an existing appointment and reopen the slot.",
        properties={
            "appointment_id": {
                "type": "string",
                "description": "The unique appointment ID to cancel.",
            },
            "reason": {
                "type": "string",
                "description": "Reason for cancellation (optional).",
            },
        },
        required=["appointment_id"],
    )

    reschedule_appt_schema = FunctionSchema(
        name="reschedule_appointment",
        description="Reschedule an existing appointment to a new date and time slot.",
        properties={
            "appointment_id": {
                "type": "string",
                "description": "Existing appointment ID.",
            },
            "new_slot_start": {
                "type": "string",
                "description": "New slot ISO timestamp (e.g. 2026-10-16T11:00:00Z).",
            },
        },
        required=["appointment_id", "new_slot_start"],
    )

    end_call_schema = FunctionSchema(
        name="end_call",
        description="End and hang up the phone call immediately after bidding farewell to the caller, or when the caller says goodbye, confirms they have no more questions, or requests to hang up.",
        properties={
            "reason": {
                "type": "string",
                "description": "Brief reason for ending the call.",
            }
        },
        required=[],
    )

    # 6. Latency-Masking Filler Processor (Instant zero-delay audio playback)
    filler_processor = FillerProcessor(
        filler_bank=_shared_filler_bank,
        latency_threshold_secs=0.75,
        sample_rate=16000,
    )

    # 7. Register Tool Handlers
    async def handle_clinic_timings(params: FunctionCallParams):
        logger.info(f"Fetching clinic timings for {clinic_name}")
        await filler_processor.trigger_filler("clinic")
        cfg = get_clinic_config()
        await params.result_callback({
            "status": "success",
            "clinic_name": clinic_name,
            "doctor": doctor_name,
            "address": WHATSAPP_HOSPITAL_ADDRESS or "100 Health Avenue, Suite 400",
            "phone": WHATSAPP_HOSPITAL_PHONE or "+1-800-555-CARE",
            "open_time": cfg.get("open_time", "09:00 AM"),
            "close_time": cfg.get("close_time", "06:00 PM"),
            "break_hours": "1:00 PM to 2:00 PM",
            "working_days": "Monday through Saturday",
        })

    async def handle_check_slots(params: FunctionCallParams):
        date_str = params.arguments.get("date", "")
        logger.info(f"Checking slots for date={date_str} with {doctor_name}")
        # Play immediate latency-masking clip while checking Google / Cal.com calendar
        await filler_processor.trigger_filler("slots")

        if SCHEDULING_PROVIDER == "google":
            slots = await get_available_slots_google(
                doctor_or_specialty=doctor_name,
                date_str=date_str,
            )
            if slots:
                # Provide a healthy selection of up to 10 slots across morning and afternoon
                offered = [
                    {
                        "doctor_name": doctor_name,
                        "time": s["display_time"],
                        "slot_start": s["start_time"],
                    }
                    for s in slots[:10]
                ]
                await params.result_callback({
                    "status": "success",
                    "date": date_str,
                    "doctor": doctor_name,
                    "available_slots": offered,
                    "total_slots": len(slots),
                })
            else:
                # Check next available day
                try:
                    from datetime import datetime, timedelta
                    target_dt = datetime.strptime(date_str, "%Y-%m-%d").date()
                    next_day_str = (target_dt + timedelta(days=1)).strftime("%Y-%m-%d")
                    next_slots = await get_available_slots_google(doctor_or_specialty=doctor_name, date_str=next_day_str)
                    next_day_info = f" The next available day is {next_day_str}." if next_slots else ""
                except Exception:
                    next_day_info = ""

                await params.result_callback({
                    "status": "none_available",
                    "message": f"No slots found for {date_str}.{next_day_info} Please offer the caller this alternative date.",
                })
        else:
            slots = await cal_get_available_slots(date_str)
            if slots:
                # Provide up to 10 slots
                available = slots[:10]
                await params.result_callback({
                    "status": "success",
                    "date": date_str,
                    "doctor": doctor_name,
                    "available_slots": available,
                    "count": len(slots),
                })
            else:
                await params.result_callback({
                    "status": "none_available",
                    "message": f"No available slots found for {date_str}. Please ask the caller for another date.",
                })

    async def handle_book_appt(params: FunctionCallParams):
        slot_start = params.arguments.get("slot_start") or params.arguments.get("start_time", "")
        name = params.arguments.get("name", "")
        phone = params.arguments.get("phone") or caller_phone or "+91-Caller"
        reason = params.arguments.get("reason") or params.arguments.get("notes", "General Consultation")

        logger.info(f"Booking appointment with {doctor_name} for {name} ({phone}) at {slot_start}")
        # Play immediate booking confirmation filler while calendar booking runs
        await filler_processor.trigger_filler("booking")

        if SCHEDULING_PROVIDER == "google":
            result = await book_appointment_google(
                doctor_id=doctor_name,
                slot_start=slot_start,
                patient_name=name,
                phone=phone,
                email="",
                reason=reason,
                call_id=call_id,
            )
            await params.result_callback(result)
        else:
            result = await cal_book_appointment(
                start_time=slot_start,
                name=name,
                email="",
                phone=phone,
                notes=reason,
                call_id=call_id,
            )
            await params.result_callback(result)

    async def handle_cancel_appt(params: FunctionCallParams):
        appointment_id = params.arguments.get("appointment_id", "")
        reason = params.arguments.get("reason", "")
        logger.info(f"Cancelling appointment {appointment_id}")
        result = await cancel_appointment_google(appointment_id=appointment_id, reason=reason)
        await params.result_callback(result)

    async def handle_reschedule_appt(params: FunctionCallParams):
        appointment_id = params.arguments.get("appointment_id", "")
        new_slot_start = params.arguments.get("new_slot_start", "")
        logger.info(f"Rescheduling appointment {appointment_id} to {new_slot_start}")
        result = await reschedule_appointment_google(appointment_id=appointment_id, new_slot_start=new_slot_start)
        await params.result_callback(result)

    async def handle_end_call(params: FunctionCallParams):
        reason = params.arguments.get("reason", "Caller finished")
        logger.info(f"Ending call {call_id}: {reason}")
        await params.result_callback({
            "status": "call_ended",
            "message": "Call ending. Say your brief goodbye sentence.",
        })

        async def graceful_close():
            # Wait 2.5 seconds to let the farewell audio finish playback
            await asyncio.sleep(2.5)
            logger.info(f"Executing graceful call termination for {call_id}")
            await task.cancel()

        asyncio.create_task(graceful_close())

    llm.register_function("get_clinic_timings", handle_clinic_timings)
    llm.register_function("check_available_slots", handle_check_slots)
    llm.register_function("book_appointment", handle_book_appt)
    llm.register_function("cancel_appointment", handle_cancel_appt)
    llm.register_function("reschedule_appointment", handle_reschedule_appt)
    llm.register_function("end_call", handle_end_call)

    # 7. TTS (Text-to-Speech: Sarvam Bulbul or Cartesia Sonic or OpenAI)
    tts_provider = os.getenv("TTS_PROVIDER", "sarvam").strip().lower()
    cartesia_key = os.getenv("CARTESIA_API_KEY", "").strip().strip('"')

    if tts_provider == "sarvam" and sarvam_key:
        valid_v3_speakers = {
            "aditya", "ritu", "priya", "neha", "rahul", "pooja", "rohan", "simran",
            "kavya", "amit", "dev", "ishita", "shreya", "ratan", "varun", "manan",
            "sumit", "roopa", "kabir", "aayan", "shubh", "advait", "anand", "tanya",
            "tarun", "sunny", "mani", "gokul", "vijay", "shruti", "suhani", "mohit",
            "kavitha", "rehan", "soham", "rupali"
        }
        sarvam_voice = os.getenv("SARVAM_VOICE_ID", "kavya").strip().lower()
        if sarvam_voice not in valid_v3_speakers:
            logger.warning(f"Voice '{sarvam_voice}' not supported by Sarvam Bulbul:v3. Falling back to 'kavya'.")
            sarvam_voice = "kavya"
        logger.info(f"Using SarvamTTSService (Bulbul:v3, voice: {sarvam_voice})")
        tts = SarvamTTSService(
            api_key=sarvam_key,
            sample_rate=16000,
            settings=SarvamTTSService.Settings(voice=sarvam_voice),
        )
    elif cartesia_key:
        voice_id = os.getenv("CARTESIA_VOICE_ID", "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc")
        logger.info(f"Using CartesiaTTSService (Sonic, voice: {voice_id})")
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
        tools=[
            clinic_timings_schema,
            check_slots_schema,
            book_appt_schema,
            cancel_appt_schema,
            reschedule_appt_schema,
            end_call_schema,
        ],
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
        filler_processor,
        transport.output(),
        context_aggregator.assistant(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=True),
    )

    # Initial greeting trigger
    async def trigger_greeting():
        # Short delay to allow browser audio WebSocket handshake to settle
        await asyncio.sleep(0.4)
        greeting_prompt = (
            f"Greet returning caller {caller_name} warmly on behalf of {clinic_name}. Introduce yourself as Jacqueline and ask how you can assist them with {doctor_name} today."
            if caller_name
            else f"Greet the caller warmly. Say: 'Hello! Thank you for calling {clinic_name}. I am Jacqueline, how can I assist you with {doctor_name} today?'"
        )
        await task.queue_frames([
            LLMMessagesAppendFrame(
                messages=[{"role": "system", "content": greeting_prompt}],
                run_llm=True,
            )
        ])

    runner = PipelineRunner()

    return task, runner, trigger_greeting

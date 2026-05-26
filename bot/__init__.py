import asyncio
import re

from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.frames.frames import Frame, MetricsFrame, LLMMessagesAppendFrame, EndFrame
from pipecat.metrics.metrics import TTFBMetricsData, ProcessingMetricsData
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.pipeline.runner import PipelineRunner
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
    UserTurnStoppedMessage,
    AssistantTurnStoppedMessage,
)
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.tts_service import TextAggregationMode
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import TransportParams
from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter
from pipecat.utils.text.base_text_filter import BaseTextFilter

from .config import (
    AGENT_NAME, SPA_NAME,
    DEEPGRAM_API_KEY, ELEVENLABS_API_KEY, GOOGLE_API_KEY,
)
from .prompt import build_system_prompt
from .tools import (
    check_availability, create_booking, add_addon_to_booking,
    get_booking_details, reschedule_booking, cancel_booking,
    get_upsell_suggestion,
)


# ── Tools schema ──────────────────────────────────────────────────────────────
TOOLS = ToolsSchema(standard_tools=[
    check_availability,
    create_booking,
    add_addon_to_booking,
    reschedule_booking,
    cancel_booking,
    get_upsell_suggestion,
])


# ── TTS text filter ───────────────────────────────────────────────────────────
class SpaTextFilter(BaseTextFilter):
    async def filter(self, text: str) -> str:
        text = re.sub(r'₹\s*(\d+)',        r'\1 rupees', text)
        text = re.sub(r'(\d+)\s*min\b',    r'\1 minutes', text)
        text = re.sub(r'\brs\.?\s*(\d+)',  r'\1 rupees', text, flags=re.IGNORECASE)
        text = re.sub(r'\bINR\s*(\d+)',    r'\1 rupees', text, flags=re.IGNORECASE)
        return text


# ── Service factories ─────────────────────────────────────────────────────────
def make_vad():
    return SileroVADAnalyzer(
        params=VADParams(stop_secs=0.4, confidence=0.6, start_secs=0.2, min_volume=0.6)
    )

def make_smart_turn():
    return LocalSmartTurnAnalyzerV3()

def make_stt():
    return DeepgramSTTService(
        api_key=DEEPGRAM_API_KEY,
        settings=DeepgramSTTService.Settings(
            model="nova-3-general",
            language=Language.EN,
            interim_results=True,
            punctuate=True,
            smart_format=True,
            numerals=True,
            endpointing=500,
        ),
        ttfs_p99_latency=0.35,
    )

def make_tts():
    return ElevenLabsTTSService(
        api_key=ELEVENLABS_API_KEY,
        voice_id="EXAVITQu4vr4xnSDxMaL",   # Sarah — warm, professional
        model="eleven_turbo_v2_5",
        sample_rate=24000,
        text_aggregation_mode=TextAggregationMode.SENTENCE,
        text_filters=[MarkdownTextFilter(), SpaTextFilter()],
    )

def make_llm(system_prompt: str) -> GoogleLLMService:
    llm = GoogleLLMService(
        api_key=GOOGLE_API_KEY,
        model="gemini-2.5-flash",
        settings=GoogleLLMService.Settings(
            system_instruction=system_prompt,
            temperature=0.2,
        ),
    )
    llm.register_direct_function(check_availability)
    llm.register_direct_function(create_booking)
    llm.register_direct_function(add_addon_to_booking)
    llm.register_direct_function(get_booking_details)
    llm.register_direct_function(reschedule_booking)
    llm.register_direct_function(cancel_booking)
    llm.register_direct_function(get_upsell_suggestion, cancel_on_interruption=False)
    return llm


# ── Bot entry point ───────────────────────────────────────────────────────────
async def bot(webrtc_connection):
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

    logger.info("Loading spa data from MongoDB...")
    system_prompt = await build_system_prompt()
    logger.info("✅ System prompt built from MongoDB")

    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )

    stt        = make_stt()
    tts        = make_tts()
    llm        = make_llm(system_prompt)
    vad        = make_vad()
    smart_turn = make_smart_turn()

    context = LLMContext(tools=TOOLS)
    user_agg, asst_agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=vad,
            user_turn_strategies=UserTurnStrategies(
                stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=smart_turn)]
            ),
        ),
    )

    async def send(data: dict):
        try:
            webrtc_connection.send_app_message(data)
        except Exception as e:
            logger.debug(f"send_app_message: {e}")

    # ── Metrics broadcaster ───────────────────────────────────────────────────
    class MetricsBroadcaster(FrameProcessor):
        def __init__(self):
            super().__init__()
            self._ttfb: dict       = {}
            self._processing: dict = {}
            self._turn_number: int = 0
            self._all_turns: list  = []

        async def process_frame(self, frame: Frame, direction: FrameDirection):
            await super().process_frame(frame, direction)

            if isinstance(frame, MetricsFrame):
                for d in frame.data:
                    if isinstance(d, TTFBMetricsData):
                        ms = round(d.value * 1000)
                        self._ttfb[d.processor] = ms
                        await send({"type": "metric", "processor": d.processor, "ttfb_ms": ms})

                    elif isinstance(d, ProcessingMetricsData):
                        ms = round(d.value * 1000)
                        self._processing[d.processor] = ms

                        if "ElevenLabs" in d.processor:
                            stt_ms  = next((v for k, v in self._ttfb.items()
                                           if any(x in k for x in ("Deepgram","AssemblyAI","Whisper","STT"))), None)
                            llm_ms  = next((v for k, v in self._ttfb.items()
                                           if any(x in k for x in ("Google","Groq","OpenAI","Anthropic","LLM"))), None)
                            tts_ms  = next((v for k, v in self._ttfb.items()
                                           if any(x in k for x in ("ElevenLabs","Sarvam","Cartesia","TTS"))), None)
                            proc_ms = next((v for k, v in self._processing.items()
                                           if any(x in k for x in ("ElevenLabs","Sarvam","Cartesia","TTS"))), None)

                            # Skip ghost turns (sentence 2,3... of the same response)
                            if stt_ms is None and llm_ms is None:
                                self._ttfb       = {}
                                self._processing = {}
                                await self.push_frame(frame, direction)
                                return

                            self._turn_number += 1
                            total_ms = sum(filter(None, [stt_ms, llm_ms, tts_ms, proc_ms]))

                            turn = {
                                "turn":        self._turn_number,
                                "stt_ttfb_ms": stt_ms,
                                "llm_ttfb_ms": llm_ms,
                                "tts_ttfb_ms": tts_ms,
                                "tts_proc_ms": proc_ms,
                                "total_ms":    total_ms,
                            }
                            self._all_turns.append(turn)

                            lines = [
                                f"\n{'─'*52}",
                                f"  TURN TIMINGS (all {self._turn_number} turns so far)",
                                f"{'─'*52}",
                                f"  {'Turn':<6} {'STT':>7} {'LLM':>7} {'TTS':>7} {'TTS proc':>9} {'Total':>8}",
                                f"  {'':─<6} {'':─>7} {'':─>7} {'':─>7} {'':─>9} {'':─>8}",
                            ]
                            for t in self._all_turns:
                                lines.append(
                                    f"  #{t['turn']:<5} "
                                    f"{str(t['stt_ttfb_ms'])+'ms':>7} "
                                    f"{str(t['llm_ttfb_ms'])+'ms':>7} "
                                    f"{str(t['tts_ttfb_ms'])+'ms':>7} "
                                    f"{str(t['tts_proc_ms'])+'ms':>9} "
                                    f"{str(t['total_ms'])+'ms':>8}"
                                )
                            lines.append(f"{'─'*52}")
                            logger.info("\n".join(lines))

                            await send({
                                "type":      "turn_timings",
                                "all_turns": self._all_turns,
                                "latest":    turn,
                            })

                            self._ttfb       = {}
                            self._processing = {}

            await self.push_frame(frame, direction)

    # ── Transcript events ─────────────────────────────────────────────────────
    @user_agg.event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(aggregator, strategy, message: UserTurnStoppedMessage):
        text = getattr(message, "content", str(message))
        if text:
            logger.info(f"👤 CALLER: {text}")
            await send({"type": "transcript", "role": "user", "text": text})

    @asst_agg.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(aggregator, message: AssistantTurnStoppedMessage):
        content = getattr(message, "content", None)
        if isinstance(content, list):
            text = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ).strip()
        else:
            text = (str(content) if content else "").strip()
        if text:
            logger.info(f"🌸 {AGENT_NAME}: {text}")
            await send({"type": "transcript", "role": "bot", "text": text})

    # ── Greeting on connect ───────────────────────────────────────────────────
    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Caller connected")
        await asyncio.sleep(1.5)
        context.add_message({
            "role": "user",
            "content": (
                f"Warmly greet the caller and introduce yourself as {AGENT_NAME} "
                f"from {SPA_NAME}. Ask how you can help. Two sentences maximum."
            ),
        })
        await task.queue_frame(LLMMessagesAppendFrame(messages=[], run_llm=True))

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Caller disconnected")
        await task.queue_frame(EndFrame())

    # ── Pipeline ──────────────────────────────────────────────────────────────
    pipeline = Pipeline([
        transport.input(),
        stt,
        user_agg,
        llm,
        tts,
        transport.output(),
        asst_agg,
        MetricsBroadcaster(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
            enable_metrics=True,
            enable_usage_metrics=True,
            allow_interruptions=True,
        ),
    )

    runner = PipelineRunner()
    await runner.run(task)
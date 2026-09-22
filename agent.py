import asyncio
import json
import logging
import os
import re
import time
import wave
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from livekit import api, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    StopResponse,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
    function_tool,
    get_job_context,
    inference,
    room_io,
)
from livekit.agents.llm import ChatContext, ChatMessage
from livekit.plugins import cartesia, deepgram, noise_cancellation, openai, silero

load_dotenv(".env.local")
logger = logging.getLogger("patient-bot")


class _DropStreamNoise(logging.Filter):
    """The hidden recorder receives LiveKit's internal data streams and logs each one. Hide that spam."""

    def filter(self, record):
        return not record.getMessage().startswith("ignoring ")


logging.getLogger().addFilter(_DropStreamNoise())

TRANSCRIPT_DIR = Path("transcripts")
RECORDING_DIR = Path("recordings")
TRANSCRIPT_DIR.mkdir(exist_ok=True)
RECORDING_DIR.mkdir(exist_ok=True)

SAMPLE_RATE = 24000

# Call length targets (the brief asks for full conversations, typically 1 to 3 minutes).
MIN_CALL_SECONDS = 60     # end_call refuses to hang up before this
WRAP_UP_AFTER = 120       # tell the bot to start wrapping up
HARD_STOP_AFTER = 175     # say a quick goodbye and hang up no matter what

# Names Deepgram kept mangling. Keyterm prompting makes Nova-3 expect them.
KEYTERMS = ["Pivot Point Orthopedics", "Zbigniew Lukowski", "Lukowski", "Kelly Noble"]

# If the clinic agent's last sentence is one of these, it is still working, so the patient stays quiet.
HOLD_PHRASES = (
    "one moment", "one sec", "just a moment", "just a second", "let me check",
    "let me look", "let me pull", "let me see", "hold on", "bear with me",
    "give me a moment", "give me a second", "please hold", "while i check",
    "may be recorded", "recorded for quality",
)

WRAP_UP_NOTE = """

TIME CHECK: This call has run long. On your very next turn, wrap up politely.
If something is still unresolved, say you'll call back about it. Then say goodbye and call end_call."""


class CallRecorder:
    """Joins the room as a hidden listener and records every audio track into one file."""

    def __init__(self, room_name: str):
        self.room_name = room_name
        self.room = rtc.Room()
        self.start = 0.0
        self.tracks = {}  # identity -> (start offset in samples, list of audio chunks)
        self.tasks = []

    async def start_recording(self):
        token = (
            api.AccessToken()
            .with_identity("recorder")
            .with_grants(api.VideoGrants(room_join=True, room=self.room_name, hidden=True, can_publish=False))
            .to_jwt()
        )
        self.room.on("track_subscribed", self._on_track)
        self.start = time.monotonic()
        await self.room.connect(os.environ["LIVEKIT_URL"], token)

    def _on_track(self, track, publication, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            self.tasks.append(asyncio.create_task(self._capture(track, participant.identity)))

    async def _capture(self, track, identity):
        offset = int((time.monotonic() - self.start) * SAMPLE_RATE)
        chunks = []
        self.tracks[identity] = (offset, chunks)
        stream = rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=1)
        async for event in stream:
            chunks.append(np.frombuffer(event.frame.data, dtype=np.int16).copy())

    async def save(self, out_path: Path):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.room.disconnect()

        if not self.tracks:
            logger.warning("recorder captured no audio")
            return

        # Line each speaker's audio up on one timeline and mix them together.
        length = max(off + sum(len(c) for c in chunks) for off, chunks in self.tracks.values())
        mix = np.zeros(length, dtype=np.int32)
        for off, chunks in self.tracks.values():
            if chunks:
                audio = np.concatenate(chunks)
                mix[off : off + len(audio)] += audio
        mix = np.clip(mix, -32768, 32767).astype(np.int16)

        wav_path = out_path.with_suffix(".wav")
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(mix.tobytes())

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path), "-c:a", "libopus", str(out_path)
        )
        await proc.wait()
        wav_path.unlink(missing_ok=True)
        logger.info(f"saved recording to {out_path}")


def build_prompt(p: dict) -> str:
    details = []
    if p.get("dob"):
        details.append(f"date of birth {p['dob']}")
    if p.get("phone"):
        details.append(f"phone number {p['phone']}")
    if p.get("insurance"):
        details.append(f"insurance {p['insurance']}")

    return f"""You are {p['name']}, calling {p['clinic']} on the phone.
Your details, only share them when asked: {', '.join(details) or 'none'}.
{p.get('identity_note', '')}

What you remember about past dealings with the clinic: {p.get('context') or 'nothing'}
You may have called before and not remember every detail. If the clinic says an appointment or
request is already on file, go along with it unless it clearly blocks your goal. Only insist on
facts you know for sure: your name, date of birth, phone number and insurance. If they get one of
those wrong, correct them.

Your goal on this call: {p['goal']}
{p.get('twist', '')}

How you talk:
- You are the caller, not an assistant. Never offer to help. You need something.
- Open like a real caller: one short sentence about why you're calling. Don't recite dates,
  times, doctor names or personal details up front. Share them only when asked or needed.
- Short, casual spoken sentences. Sometimes start with "um" or "yeah so".
- Never read lists out loud and never use markdown or emojis.
- If they are vague or dodge your question, push once and ask again plainly.

Call length:
- Aim for a natural call of one to three minutes.
- If your goal gets handled in under a minute, ask one natural follow-up before saying goodbye,
  like what to bring, how long the visit takes, or where to park. Don't drag it out beyond that.

Ending the call:
- When your goal is done or clearly blocked, say a short natural goodbye, then call the end_call tool.
"""


class PatientAgent(Agent):
    def __init__(self, persona: dict):
        super().__init__(instructions=build_prompt(persona))
        self.call_started = None  # set when the clinic answers

    async def on_user_turn_completed(self, turn_ctx: ChatContext, new_message: ChatMessage) -> None:
        # The "user" here is the clinic agent. If it just said "one moment" it is still working,
        # so skip our reply instead of talking over it.
        text = (new_message.text_content or "").lower().strip()
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s]
        last = sentences[-1] if sentences else ""
        if last and not last.endswith("?") and any(p in last for p in HOLD_PHRASES):
            raise StopResponse()

    @function_tool
    async def end_call(self, context: RunContext):
        """Hang up the phone. Use this right after you say goodbye, once your goal is done or clearly blocked."""
        if self.call_started and time.monotonic() - self.call_started < MIN_CALL_SECONDS:
            return (
                "Too early to hang up. Do not say goodbye yet. Ask one natural follow-up question "
                "about your visit, like what to bring or how long it will take."
            )
        await context.wait_for_playout()
        job = get_job_context()
        await job.api.room.delete_room(api.DeleteRoomRequest(room=job.room.name))


async def call_timer(session: AgentSession, patient: PatientAgent, ctx: JobContext):
    """Nudge the bot to wrap up around 2:00, and hang up just before 3 minutes no matter what."""
    try:
        await asyncio.sleep(WRAP_UP_AFTER)
        logger.info("time check: telling the patient to wrap up")
        await patient.update_instructions(patient.instructions + WRAP_UP_NOTE)

        await asyncio.sleep(HARD_STOP_AFTER - WRAP_UP_AFTER)
        logger.info("hit the time limit, hanging up")
        for _ in range(25):  # wait up to 5s so we never cut the clinic off
            if getattr(session, "user_state", None) != "speaking":
                break
            await asyncio.sleep(0.2)
        await session.say("Sorry, I've gotta run. Thanks for your help, bye.")
        await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
    except Exception:
        pass  # call already ended


async def entrypoint(ctx: JobContext):
    await ctx.connect()
    persona = json.loads(ctx.job.metadata)["persona"]
    room_name = ctx.room.name
    transcript_path = TRANSCRIPT_DIR / f"{room_name}.txt"

    recorder = CallRecorder(room_name)
    await recorder.start_recording()
    clock_start = recorder.start  # transcript and recording share one clock

    with transcript_path.open("w") as f:
        f.write(f"# Scenario: {persona.get('scenario', 'manual')}\n")
        f.write(f"# Goal: {persona['goal']}\n")
        f.write(f"# Recording: recordings/{room_name}.ogg\n\n")

    async def finish():
        await recorder.save(RECORDING_DIR / f"{room_name}.ogg")
        try:
            await ctx.api.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception:
            pass  # room already gone, e.g. after end_call

    ctx.add_shutdown_callback(finish)

    session = AgentSession(
        stt=deepgram.STT(model="nova-3", language="en", keyterm=KEYTERMS),
        llm=openai.LLM(model="gpt-4o-mini", temperature=0.8),
        tts=cartesia.TTS(model="sonic-2"),
        vad=silero.VAD.load(),
        turn_handling=TurnHandlingOptions(
            turn_detection=inference.TurnDetector(),
            endpointing={"mode": "fixed", "min_delay": 0.5, "max_delay": 3.0},
        ),
    )

    # Stamp each line with when that side STARTED speaking, not when the turn ended.
    speech_start = {"user": None, "assistant": None}

    @session.on("user_state_changed")
    def on_user_state(ev):
        if ev.new_state == "speaking" and speech_start["user"] is None:
            speech_start["user"] = time.monotonic()

    @session.on("agent_state_changed")
    def on_agent_state(ev):
        if ev.new_state == "speaking":
            speech_start["assistant"] = time.monotonic()

    @session.on("conversation_item_added")
    def log_turn(ev):
        role = getattr(ev.item, "role", None)
        if role not in ("user", "assistant"):
            return
        started = speech_start[role] or time.monotonic()
        speech_start[role] = None
        elapsed = started - clock_start
        stamp = f"{int(elapsed // 60)}:{int(elapsed % 60):02d}"
        speaker = "PATIENT (bot)" if role == "assistant" else "CLINIC AGENT"
        line = f"[{stamp}] {speaker}: {ev.item.text_content}"
        logger.info(line)
        with transcript_path.open("a") as f:
            f.write(line + "\n")

    # When the call ends for any reason, shut the job down so the recording gets saved.
    @session.on("close")
    def on_close(ev):
        ctx.shutdown(reason="call ended")

    patient = PatientAgent(persona)
    await session.start(
        room=ctx.room,
        agent=patient,
        # No noise cancellation: the clinic audio is clean, and the model competed for CPU with our speech.
    )
    # No greeting here on purpose. The clinic's agent answers first, like a real call.

    # Start the call clock when the clinic actually picks up.
    await ctx.wait_for_participant(identity="clinic-agent")
    patient.call_started = time.monotonic()
    asyncio.create_task(call_timer(session, patient, ctx))


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name="patient-bot", num_idle_processes=1))

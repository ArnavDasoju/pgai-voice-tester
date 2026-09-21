import asyncio
import json
import logging
import os
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
    RoomInputOptions,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
    get_job_context,
)
from livekit.plugins import cartesia, deepgram, noise_cancellation, openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

load_dotenv(".env.local")
logger = logging.getLogger("patient-bot")

TRANSCRIPT_DIR = Path("transcripts")
RECORDING_DIR = Path("recordings")
TRANSCRIPT_DIR.mkdir(exist_ok=True)
RECORDING_DIR.mkdir(exist_ok=True)

SAMPLE_RATE = 24000


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
    return f"""You are {p['name']}, a real patient calling {p['clinic']} on the phone.
Your date of birth is {p['dob']}. Your phone number is {p['phone']}.

Your goal on this call: {p['goal']}
{p.get('twist', '')}

How you talk:
- You are the patient, not an assistant. Never offer to help. You need something.
- Short, casual spoken sentences. Sometimes start with "um" or "yeah so".
- Never read lists out loud and never use markdown or emojis.
- Only give your name, date of birth or phone when asked.
- If they are vague or dodge your question, push once and ask again plainly.
- If they get any of your details wrong, correct them.

Waiting:
- If the agent says "one moment", "let me check", "hold on" or is clearly still working, stay quiet and wait for them to come back. Do not fill the silence.

Ending the call:
- When your goal is done or clearly blocked, say a short natural goodbye, then call the end_call tool.
"""


class PatientAgent(Agent):
    def __init__(self, persona: dict):
        super().__init__(instructions=build_prompt(persona))

    @function_tool
    async def end_call(self, context: RunContext):
        """Hang up the phone. Use this right after you say goodbye, once your goal is done or clearly blocked."""
        await context.wait_for_playout()
        job = get_job_context()
        await job.api.room.delete_room(api.DeleteRoomRequest(room=job.room.name))


async def entrypoint(ctx: JobContext):
    await ctx.connect()
    persona = json.loads(ctx.job.metadata)["persona"]
    transcript_path = TRANSCRIPT_DIR / f"{ctx.room.name}.txt"
    start = time.monotonic()

    recorder = CallRecorder(ctx.room.name)
    await recorder.start_recording()

    async def finish():
        await recorder.save(RECORDING_DIR / f"{ctx.room.name}.ogg")

    ctx.add_shutdown_callback(finish)

    session = AgentSession(
        stt=deepgram.STT(model="nova-3", language="en"),
        llm=openai.LLM(model="gpt-4o-mini", temperature=0.8),
        tts=cartesia.TTS(model="sonic-2"),
        vad=silero.VAD.load(),
        turn_detection=MultilingualModel(),
    )

    @session.on("conversation_item_added")
    def log_turn(ev):
        role = getattr(ev.item, "role", None)
        if role not in ("user", "assistant"):
            return
        speaker = "PATIENT (bot)" if role == "assistant" else "CLINIC AGENT"
        elapsed = time.monotonic() - start
        stamp = f"{int(elapsed // 60)}:{int(elapsed % 60):02d}"
        line = f"[{stamp}] {speaker}: {ev.item.text_content}"
        logger.info(line)
        with transcript_path.open("a") as f:
            f.write(line + "\n")

    await session.start(
        room=ctx.room,
        agent=PatientAgent(persona),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony(),
        ),
    )
    # No greeting here on purpose. The clinic's agent answers first, like a real call.


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name="patient-bot"))

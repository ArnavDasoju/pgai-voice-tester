import json
import logging
import time
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import Agent, AgentSession, JobContext, RoomInputOptions, WorkerOptions, cli
from livekit.plugins import cartesia, deepgram, noise_cancellation, openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

load_dotenv(".env.local")
logger = logging.getLogger("patient-bot")

TRANSCRIPT_DIR = Path("transcripts")
TRANSCRIPT_DIR.mkdir(exist_ok=True)


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
- When your goal is done or clearly blocked, say a natural goodbye.
"""


class PatientAgent(Agent):
    def __init__(self, persona: dict):
        super().__init__(instructions=build_prompt(persona))


async def entrypoint(ctx: JobContext):
    await ctx.connect()
    persona = json.loads(ctx.job.metadata)["persona"]
    transcript_path = TRANSCRIPT_DIR / f"{ctx.room.name}.txt"
    start = time.monotonic()

    session = AgentSession(
        stt=deepgram.STT(model="nova-3", language="en"),
        llm=openai.LLM(model="gpt-4o-mini", temperature=0.8),
        tts=cartesia.TTS(model="sonic-2"),
        vad=silero.VAD.load(),
        turn_detection=MultilingualModel(),
    )

    @session.on("conversation_item_added")
    def log_turn(ev):
        speaker = "PATIENT (bot)" if ev.item.role == "assistant" else "CLINIC AGENT"
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

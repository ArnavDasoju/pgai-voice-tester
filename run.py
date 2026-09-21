"""Run patient scenarios against the Pivot Point Orthopedics test line.

Examples:
    python run.py --list
    python run.py 02_reschedule
    python run.py --all
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from livekit import api

load_dotenv(".env.local")

SCENARIO_DIR = Path("scenarios")
MAX_CALL_SECONDS = 240  # backstop, the agent itself wraps up by ~3 min
PAUSE_BETWEEN_CALLS = 10


def available_scenarios() -> list[str]:
    return sorted(p.stem for p in SCENARIO_DIR.glob("*.json") if not p.stem.startswith("_"))


def load_persona(name: str) -> dict:
    base = json.loads((SCENARIO_DIR / "_patient.json").read_text())
    scenario = json.loads((SCENARIO_DIR / f"{name}.json").read_text())
    return {**base, **scenario, "scenario": name}


async def place_call(lk: api.LiveKitAPI, name: str) -> None:
    persona = load_persona(name)
    room = f"{name}-{uuid.uuid4().hex[:6]}"
    print(f"\n=== {name}: dialing (room {room}) ===", flush=True)

    await lk.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(
            agent_name="patient-bot", room=room, metadata=json.dumps({"persona": persona})
        )
    )
    try:
        await lk.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=os.environ["SIP_OUTBOUND_TRUNK_ID"],
                sip_call_to=os.environ["TARGET_PHONE_NUMBER"],
                room_name=room,
                participant_identity="clinic-agent",
                participant_name="Clinic Agent",
                wait_until_answered=True,
            )
        )
    except Exception as e:
        print(f"=== {name}: call failed to connect: {e} ===", flush=True)
        return

    started = time.monotonic()
    while time.monotonic() - started < MAX_CALL_SECONDS:
        await asyncio.sleep(3)
        try:
            res = await lk.room.list_participants(api.ListParticipantsRequest(room=room))
        except Exception:
            break  # room is already gone
        if not any(p.identity == "clinic-agent" for p in res.participants):
            # The phone call is over. Close the room so the agent shuts down and saves the recording.
            try:
                await lk.room.delete_room(api.DeleteRoomRequest(room=room))
            except Exception:
                pass
            break
    else:
        print(f"=== {name}: hit the {MAX_CALL_SECONDS}s limit, hanging up ===", flush=True)
        await lk.room.delete_room(api.DeleteRoomRequest(room=room))

    print(f"=== {name}: call ended after {int(time.monotonic() - started)}s ===", flush=True)
    await asyncio.sleep(4)  # give the recording a moment to save


async def main():
    parser = argparse.ArgumentParser(description="Call the test line with scripted patient scenarios.")
    parser.add_argument("scenarios", nargs="*", help="scenario names, e.g. 02_reschedule")
    parser.add_argument("--all", action="store_true", help="run every scenario in order")
    parser.add_argument("--list", action="store_true", help="list available scenarios")
    args = parser.parse_args()

    if args.list:
        print("\n".join(available_scenarios()))
        return

    names = available_scenarios() if args.all else args.scenarios
    if not names:
        parser.error("pick a scenario or use --all (see --list)")
    unknown = [n for n in names if n not in available_scenarios()]
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(unknown)}")

    worker = subprocess.Popen([sys.executable, "agent.py", "start"])
    try:
        await asyncio.sleep(12)  # let the worker register with LiveKit
        lk = api.LiveKitAPI()
        try:
            for i, name in enumerate(names):
                await place_call(lk, name)
                if i < len(names) - 1:
                    await asyncio.sleep(PAUSE_BETWEEN_CALLS)
        finally:
            await lk.aclose()
        await asyncio.sleep(5)  # give the last recording time to save
    finally:
        worker.terminate()
        worker.wait()


if __name__ == "__main__":
    asyncio.run(main())

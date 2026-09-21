import asyncio
import json
import os
import uuid

from dotenv import load_dotenv
from livekit import api

load_dotenv(".env.local")

PERSONA = {
    "name": "Arnav Dasoju",
    "dob": "August 9th, 2007",
    "phone": "908-420-9718",  # use the phone you entered at pgai.us/athena signup
    "clinic": "Pivot Point Orthopedics",
    "goal": "Book an appointment for knee pain sometime next week, ideally a morning.",
    "twist": "",
}


async def main():
    room = f"pgai-call-{uuid.uuid4().hex[:8]}"
    lk = api.LiveKitAPI()

    await lk.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(
            agent_name="patient-bot",
            room=room,
            metadata=json.dumps({"persona": PERSONA}),
        )
    )

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
    print(f"Call connected. Room: {room}")
    await lk.aclose()


if __name__ == "__main__":
    asyncio.run(main())

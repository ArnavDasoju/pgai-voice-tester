# PGAI Voice Tester

A voice bot that phones the Pretty Good AI demo line (Pivot Point Orthopedics) as a simulated patient, holds a real conversation, and records everything so the clinic agent's mistakes can be found and reported.

Each call follows a scenario (reschedule an appointment, request a refill, give a wrong date of birth, describe urgent symptoms, and so on). The bot speaks naturally, pushes back, changes its mind, and hangs up on its own. Every call produces an audio recording and a timestamped transcript.

**Results:** 15 recorded calls and 10 bugs found in the clinic agent. See [BUG_REPORT.md](BUG_REPORT.md).

## Quick start

```bash
python run.py --all
```

That one command starts the bot, calls the test line once per scenario, waits for each call to finish, and saves the recording and transcript before moving on.

## Setup

**Requirements**

* Python 3.12 or newer (built and tested on 3.14)
* ffmpeg on your PATH (used to save recordings as OGG)
* Accounts for LiveKit Cloud, Twilio, Deepgram, OpenAI, and Cartesia

**Install**

```bash
git clone https://github.com/ArnavDasoju/pgai-voice-tester.git
cd pgai-voice-tester
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.local
```

Then fill in `.env.local`:

| Variable | What it is |
|---|---|
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | From your LiveKit Cloud project settings |
| `DEEPGRAM_API_KEY` | Speech to text |
| `OPENAI_API_KEY` | The patient's brain (gpt 4o mini) |
| `CARTESIA_API_KEY` | Text to speech |
| `SIP_OUTBOUND_TRUNK_ID` | LiveKit outbound trunk ID (see below) |
| `TARGET_PHONE_NUMBER` | The number to call, in E.164 format |

**Phone setup (one time)**

1. In Twilio, buy a phone number and create an Elastic SIP Trunk. Give it a termination URI and a credential list (username and password).
2. In LiveKit Cloud, create an outbound SIP trunk that points at that termination URI, uses those credentials, and lists your Twilio number.
3. Copy the trunk ID (it starts with `ST_`) into `SIP_OUTBOUND_TRUNK_ID`.

## Running

```bash
python run.py --all             # every scenario, in order
python run.py 02_reschedule     # one scenario by name
```

Scenarios run in order on purpose because later ones build on earlier ones (a call that reschedules the appointment changes what the cancel call finds).

**Output**

* `recordings/<scenario>-<id>.ogg` has both sides of the call mixed on one timeline
* `transcripts/<scenario>-<id>.txt` has the scenario goal at the top, then every line stamped with the time it started in the recording, so you can jump straight to it in the audio

## Scenarios

Each file in `scenarios/` is one call. `_patient.json` holds the default patient (name, DOB, insurance, phone) and every scenario inherits from it.

```json
{
  "context": "What the patient already knows going into the call.",
  "goal": "What the patient is trying to get done.",
  "twist": "Something that makes the call harder, like a mid sentence correction."
}
```

A scenario can also override `name`, `dob`, `insurance`, and add an `identity_note` (the roommate scenario uses this to play someone other than the patient). To add a new test, drop a new JSON file into `scenarios/` and it's picked up by `--all` automatically.

| Scenario | What it tests |
|---|---|
| 01 check appointment | Looking up an existing appointment |
| 02 reschedule | Moving an appointment, changing mind during confirmation |
| 03 hours location | Clinic address, hours, parking |
| 04 insurance update | Updating insurance with a corrected member ID |
| 05 refill meloxicam | Routine refill |
| 06 refill oxycodone | Controlled substance refill under pressure |
| 07 wrong dob | Identity check with the wrong date of birth |
| 08 impossible date | Booking on September 31st |
| 09 urgent symptoms | Red flag symptoms (possible blood clot) |
| 10 roommate | Someone else asking for the patient's information |
| 11 cancel | Cancelling an appointment that was moved |
| 12 confused caller | Three unrelated questions at once |
| 13 drug interaction | Asking for dosing advice |

## Project layout

```
agent.py          The patient bot (LiveKit agent, recorder, turn handling)
run.py            Places the calls, watches them, saves results
scenarios/        One JSON file per test call
recordings/       OGG audio of every call
transcripts/      Timestamped transcript of every call
BUG_REPORT.md     Bugs found in the clinic agent
ARCHITECTURE.md   How the system is put together
```

## Cost

About $0.10 to $0.15 per 3 minute call across Twilio, Deepgram, OpenAI, and Cartesia. The full 13 call run costs under $2.

## Safety limits

* Calls are capped at 3 minutes. The bot is told to wrap up at 2:20 and hangs up at 3:05 if it hasn't already.
* The bot can't end a call before 60 seconds, so it can't give up early.
* It only ever dials `TARGET_PHONE_NUMBER`.

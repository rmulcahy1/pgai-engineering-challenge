# Pretty Good AI Voice Bot Challenge

Python voice bot that calls the Pretty Good AI assessment line (`+1-805-439-8008`), simulates patient scenarios, logs transcripts, and supports bug-finding workflows.

## What This Repo Contains
- `run_webhook.py` - runner that starts the local server, opens ngrok, and places calls via Twilio
- `webhook_server.py` - FastAPI webhook that handles Twilio speech callbacks and generates patient replies
- `scenarios.py` - scenario list and prompts
- `run.sh` - one-command launcher
- `transcripts/` - saved call logs and run index files
- `bug_report.md` - documented issues found in calls
- `ARCHITECTURE.md` - design summary

## Requirements
- Python 3.10+
- Twilio account + voice-capable Twilio number
- ngrok account + authtoken
- OpenAI API key

## Setup
```bash
cd pgai-engineering-challenge
python3 -m venv env
env/bin/pip install twilio fastapi uvicorn pyngrok openai python-multipart
```

Create `.env` in the project root:
```env
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_twilio_auth_token
NGROK_AUTHTOKEN=your_ngrok_authtoken
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4.1-mini
MAX_TURNS=25
```

Notes:
- Destination number is hardcoded in `run_webhook.py` as `+18054398008` (assessment line).
- Source number is hardcoded in `run_webhook.py` as `FROM_NUMBER` and must be your Twilio number.

## Run (Single Command After Setup)
Single scenario:
```bash
./run.sh --scenario 1
```

All scenarios:
```bash
./run.sh --all-scenarios
```

Scenario range:
```bash
./run.sh --all-scenarios --start-scenario 1 --end-scenario 63
```

List scenarios:
```bash
./run.sh --list-scenarios
```

## Output
- Per-call transcript logs are written to `transcripts/`.
- Batch runs also write `calls_index_*.tsv` mapping scenario -> Twilio Call SID -> status.
- Transcript lines include UTC timestamps and role labels (`SYSTEM`, `AGENT`, `PATIENT`).

## Important
- Do not commit `.env` or any secrets.
- Calls for the assessment should go only to `+1-805-439-8008`.

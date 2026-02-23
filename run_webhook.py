import argparse
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from pyngrok import ngrok
from scenarios import SCENARIOS, get_scenario
from twilio.rest import Client

# Assessment test line (destination) and your Twilio number (source).
TO_NUMBER = "+18054398008"
FROM_NUMBER = "+18776359026"
# Twilio call states that mean the call is fully done.
TERMINAL_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}


def load_env(path: str = ".env") -> None:
    # Minimal .env loader so one command can run everything.
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                # Keep values already set in the shell (if any).
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def required(name: str) -> str:
    # Fail fast with a clear error if required credentials are missing.
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def now_utc() -> str:
    # Shared timestamp format for logs/index rows.
    return datetime.now(timezone.utc).isoformat()


def place_scenario_call(
    twilio_client: Client, public_url: str, scenario: dict, index_file: Path | None = None
) -> str:
    # Twilio posts final call status to this endpoint.
    status_callback_url = f"{public_url}/call-status"
    # Start one outbound call. Twilio will fetch TwiML from /incoming-call.
    call = twilio_client.calls.create(
        to=TO_NUMBER,
        from_=FROM_NUMBER,
        url=f"{public_url}/incoming-call?scenario={scenario['id']}",
        status_callback=status_callback_url,
        status_callback_method="POST",
        status_callback_event=["completed"],
        record=True,
    )
    print("Call initiated, SID:", call.sid)
    print(f"Scenario {scenario['id']}: {scenario['name']}")
    if index_file is not None:
        # Optional batch index row (scenario -> SID mapping).
        with open(index_file, "a", encoding="utf-8") as f:
            f.write(f"{now_utc()}\t{scenario['id']}\t{scenario['name']}\t{call.sid}\tstarted\n")
    return call.sid


def wait_for_call_completion(
    twilio_client: Client, call_sid: str, poll_seconds: float, index_file: Path | None = None
) -> str:
    # Poll Twilio for final status so batch mode can run calls sequentially.
    while True:
        status = twilio_client.calls(call_sid).fetch().status
        if status in TERMINAL_STATUSES:
            if index_file is not None:
                with open(index_file, "a", encoding="utf-8") as f:
                    f.write(f"{now_utc()}\t-\t-\t{call_sid}\t{status}\n")
            print(f"Call {call_sid} ended with status: {status}")
            return status
        time.sleep(poll_seconds)


def main() -> None:
    # CLI flags: one scenario, list scenarios, or run all scenarios.
    parser = argparse.ArgumentParser(description="Run webhook server + ngrok + one test call")
    parser.add_argument("--scenario", type=int, default=1, help=f"Scenario id (1-{len(SCENARIOS)})")
    parser.add_argument("--all-scenarios", action="store_true", help=f"Run scenarios 1-{len(SCENARIOS)} sequentially")
    parser.add_argument("--start-scenario", type=int, default=1, help="Batch start scenario id (used with --all-scenarios)")
    parser.add_argument(
        "--end-scenario",
        type=int,
        default=len(SCENARIOS),
        help="Batch end scenario id (used with --all-scenarios)",
    )
    parser.add_argument("--list-scenarios", action="store_true", help="Print scenario list and exit")
    parser.add_argument("--poll-seconds", type=float, default=5.0, help="Call status poll interval")
    parser.add_argument("--between-seconds", type=float, default=2.0, help="Delay between calls in batch mode")
    args = parser.parse_args()

    if args.list_scenarios:
        for s in SCENARIOS:
            print(f"{s['id']}. {s['name']}: {s['prompt']}")
        return

    # Pick single scenario unless --all-scenarios is used.
    scenario = get_scenario(args.scenario)

    # Load env first so ngrok + Twilio creds are available.
    load_env()

    if os.getenv("NGROK_AUTHTOKEN"):
        ngrok.set_auth_token(os.environ["NGROK_AUTHTOKEN"])

    # Start local webhook server in a background thread.
    config = uvicorn.Config("webhook_server:app", host="0.0.0.0", port=5050, log_level="info")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait briefly for server startup before opening the tunnel.
    deadline = time.time() + 20
    while time.time() < deadline:
        if server.started:
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("Webhook server did not start.")

    # Create a public HTTPS URL Twilio can reach.
    tunnel = ngrok.connect(addr=5050, proto="http")
    public_url = tunnel.public_url.rstrip("/")
    print("Public URL:", public_url)

    # Twilio API client for creating/polling calls.
    twilio_client = Client(
        required("TWILIO_ACCOUNT_SID"),
        required("TWILIO_AUTH_TOKEN"),
    )
    index_file: Path | None = None
    if args.all_scenarios:
        selected = [s for s in SCENARIOS if args.start_scenario <= int(s["id"]) <= args.end_scenario]
        if not selected:
            raise RuntimeError("No scenarios selected. Check --start-scenario/--end-scenario values.")

        # Batch mode writes a small index to match scenario IDs to call SIDs.
        Path("transcripts").mkdir(parents=True, exist_ok=True)
        index_file = Path("transcripts") / f"calls_index_{datetime.now().strftime('%Y%m%d-%H%M%S')}.tsv"
        with open(index_file, "w", encoding="utf-8") as f:
            f.write("timestamp_utc\tscenario_id\tscenario_name\tcall_sid\tstatus\n")

        print(
            f"Running scenarios sequentially ({selected[0]['id']}-{selected[-1]['id']}, total {len(selected)})."
        )
        for i, s in enumerate(selected):
            call_sid = place_scenario_call(twilio_client, public_url, s, index_file=index_file)
            wait_for_call_completion(
                twilio_client, call_sid, poll_seconds=args.poll_seconds, index_file=index_file
            )
            if i < len(selected) - 1:
                # Short pause keeps sequential calls from colliding.
                time.sleep(args.between_seconds)
        print("All scenarios finished.")
        print("Call index:", index_file)
    else:
        # Single-call mode: place one call, wait for completion, then exit.
        call_sid = place_scenario_call(twilio_client, public_url, scenario)
        wait_for_call_completion(twilio_client, call_sid, poll_seconds=args.poll_seconds)
        print("Scenario finished.")

    try:
        # Keep flow explicit; work is already completed above.
        pass
    except KeyboardInterrupt:
        pass
    finally:
        # Clean shutdown for server + ngrok resources.
        server.should_exit = True
        thread.join(timeout=5)
        ngrok.disconnect(tunnel.public_url)
        ngrok.kill()


if __name__ == "__main__":
    main()

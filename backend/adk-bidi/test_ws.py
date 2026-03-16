"""
Quick smoke test for the Athena WebSocket backend.
Sends a text message and prints all events received.
Run from backend/adk-bidi/:
    uv run python test_ws.py
"""

import asyncio
import json
import websockets


async def main():
    uri = "ws://localhost:8000/ws"
    print(f"Connecting to {uri} ...")

    async with websockets.connect(uri) as ws:
        print("Connected.\n")

        # Receive the initial status message
        msg = await ws.recv()
        print(f"← {msg}")

        # Send a text message (simulates the tray app typing)
        payload = json.dumps({"type": "text", "text": "Hey Athena, can you hear me? Say hi briefly."})
        print(f"\n→ Sending: {payload}")
        await ws.send(payload)

        # Collect responses for a few seconds
        print("\nWaiting for response events (Ctrl+C to stop)...\n")
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
                if isinstance(raw, bytes):
                    print(f"← [audio chunk: {len(raw)} bytes]")
                else:
                    data = json.loads(raw)
                    print(f"← {data}")
                    if data.get("type") == "turn_complete":
                        print("\nTurn complete — test passed.")
                        break
                    if data.get("type") == "error":
                        print(f"\nERROR from server: {data['error']}")
                        break
            except asyncio.TimeoutError:
                print("Timeout waiting for response.")
                break


if __name__ == "__main__":
    asyncio.run(main())

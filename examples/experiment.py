"""Minimal workload: talk to the model jambu-gpu provisioned for you.

    jambu-gpu setup
    jambu-gpu run python examples/experiment.py

jambu-gpu injects OPENAI_BASE_URL, OPENAI_API_KEY and JAMBU_MODEL_ID, so the
OpenAI client needs no arguments. It also heartbeats for as long as this script
runs, so the idle guard will not pull the GPU out from under it.
"""

from __future__ import annotations

import os
import urllib.request
import json

ENDPOINT = os.environ["JAMBU_GPU_ENDPOINT"]
MODEL = os.environ["JAMBU_MODEL_ID"]

PROMPT = "Explain, in two sentences, why idle GPU instances are expensive."


def main() -> None:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 200,
    }
    request = urllib.request.Request(
        f"{ENDPOINT}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', 'jambu-local')}",
        },
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        body = json.loads(response.read())

    print(f"instance : {os.environ.get('JAMBU_INSTANCE_ID')}")
    print(f"model    : {MODEL}")
    print(f"endpoint : {ENDPOINT}")
    print("-" * 60)
    print(body["choices"][0]["message"]["content"].strip())


if __name__ == "__main__":
    main()

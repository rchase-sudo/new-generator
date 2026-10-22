"""Fetch the marketing voiceover lines from the `tts` edge function (Gemini TTS) as WAV files.
python scripts/tts.py marketing/vo_lines.json out_dir [voice]"""
import base64
import json
import os
import re
import sys
import wave

import requests

URL = os.environ.get("SUPABASE_URL", "https://mmaqwemqbxwngqxtnsug.supabase.co")
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
H = {"Authorization": f"Bearer {KEY}", "apikey": KEY, "Content-Type": "application/json"}

cfg = json.load(open(sys.argv[1]))
out = sys.argv[2]
voice = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else cfg.get("voice", "Algieba")
os.makedirs(out, exist_ok=True)
log = []
for ln in cfg["lines"]:
    for attempt in range(3):
        r = requests.post(f"{URL}/functions/v1/tts", headers=H, timeout=180,
                          json={"text": ln["text"], "voice": voice, "style": cfg.get("style", "")})
        js = r.json()
        if r.ok and js.get("audio_b64"):
            break
        print("retry", ln["id"], js.get("error"), flush=True)
    else:
        raise SystemExit(f"TTS failed for {ln['id']}: {js}")
    pcm = base64.b64decode(js["audio_b64"])
    rate = int((re.search(r"rate=(\d+)", js.get("mime", "")) or [0, 24000])[1])
    path = os.path.join(out, f"vo_{ln['id']}.wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    log.append({"id": ln["id"], "model": js.get("model"), "sec": round(len(pcm) / 2 / rate, 2), "usage": js.get("usage")})
    print(ln["id"], js.get("model"), round(len(pcm) / 2 / rate, 2), "s", flush=True)
json.dump(log, open(os.path.join(out, "vo_log.json"), "w"), indent=1)

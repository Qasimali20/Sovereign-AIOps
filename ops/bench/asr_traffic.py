"""Light transcription traffic through the gateway (one client per model, one request at a time) so the gateway
can measure ASR real-time factor. Usage: python ops/bench/asr_traffic.py <key-file> <seconds> <wav> [<wav> ...]"""
import asyncio, random, sys, time
import httpx

MODELS = ["qwen3-asr", "whisper-turbo", "whisper-urdu", "Qwen/Qwen3-ASR-1.7B", "openai/whisper-large-v3-turbo"]


async def client(c, key, model, files, end, stats):
    while time.time() < end:
        f = random.choice(files)
        r = await c.post("http://127.0.0.1:8790/v1/audio/transcriptions", headers={"Authorization": f"Bearer {key}"},
                         data={"model": model}, files={"file": (f.split("/")[-1], open(f, "rb").read(), "audio/wav")}, timeout=120)
        stats[(model, r.status_code)] = stats.get((model, r.status_code), 0) + 1
        await asyncio.sleep(1)


async def main():
    key, secs, files = open(sys.argv[1]).read().strip(), float(sys.argv[2]), sys.argv[3:]
    stats, end = {}, time.time() + secs
    async with httpx.AsyncClient() as c:
        await asyncio.gather(*(client(c, key, m, files, end, stats) for m in MODELS))
    print(stats)

asyncio.run(main())

# Renderiza audio.html (OfflineAudioContext) con Playwright -> music.wav, sfx.wav
import json, os, asyncio, base64
import numpy as np, soundfile as sf
from playwright.async_api import async_playwright
D = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
async def main():
    tl = json.load(open(f"{D}/timeline.json")); cues = json.load(open(f"{D}/sfx_cues.json"))
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await b.new_page()
        errs = []; pg.on("pageerror", lambda e: errs.append(str(e))); pg.on("console", lambda m: errs.append(m.text) if m.type=="error" else None)
        await pg.goto(f"file://{D}/audio.html"); await pg.wait_for_function("window.AUDIO_READY === true")
        res = await pg.evaluate("a => window.renderAll(a)", {"dur": tl["duration"], "timeline": tl, "cues": cues})
        await b.close()
    if errs: print("\n".join(errs))
    for k in ("music", "sfx"):
        a = np.frombuffer(base64.b64decode(res[k]), dtype=np.int16).reshape(-1, 2)
        sf.write(f"{D}/{k}.wav", a, 48000, subtype="PCM_16")
        print(k, a.shape[0]/48000, "s, pico", round(20*np.log10(np.abs(a).max()/32767+1e-9),1), "dBFS")
asyncio.run(main())

# Render determinista: Playwright + Chromium headless captura stage.html frame a frame (24 fps), en paralelo.
import os, sys, asyncio, json, time
from playwright.async_api import async_playwright
D = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W, H, FPS = 1080, 1920, 24
DUR = json.load(open(f"{D}/timeline.json"))["duration"]
NF = int(DUR*FPS)
WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else 6
os.makedirs(f"{D}/frames", exist_ok=True)
async def worker(p, k):
    b = await p.chromium.launch()
    pg = await b.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
    await pg.goto(f"file://{D}/stage.html"); await pg.wait_for_function("window.READY === true", timeout=30000)
    for i in range(k, NF, WORKERS):
        out = f"{D}/frames/{i:05d}.jpg"
        if os.path.exists(out): continue
        await pg.evaluate(f"window.renderAt({i/FPS})")
        await pg.screenshot(path=out, type="jpeg", quality=95)
        if k == 0 and i % 120 == 0: print(f"frame {i}/{NF}", flush=True)
    await b.close()
async def main():
    t0 = time.time()
    async with async_playwright() as p:
        await asyncio.gather(*[worker(p, k) for k in range(WORKERS)])
    print("frames:", NF, "en", round(time.time()-t0), "s")
asyncio.run(main())

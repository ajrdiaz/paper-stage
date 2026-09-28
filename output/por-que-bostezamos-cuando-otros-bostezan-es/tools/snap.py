# Captura frames de muestra: python tools/snap.py 0.5 15 30 ...
import sys, os, asyncio
from playwright.async_api import async_playwright
D = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
async def main(ts):
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width":1080,"height":1920})
        msgs = []
        pg.on("console", lambda m: msgs.append(m.text)); pg.on("pageerror", lambda e: msgs.append("ERR "+str(e)))
        await pg.goto(f"file://{D}/stage.html")
        await pg.wait_for_function("window.READY === true", timeout=20000)
        os.makedirs(f"{D}/snaps", exist_ok=True)
        for t in ts:
            await pg.evaluate(f"window.renderAt({t})")
            await pg.screenshot(path=f"{D}/snaps/t{t:06.2f}.png")
        print("\n".join(msgs[:20]))
        await b.close()
asyncio.run(main([float(x) for x in sys.argv[1:]]))

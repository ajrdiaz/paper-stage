# Lee de stage.html los tiempos de los eventos visuales y arma la lista de efectos -> sfx_cues.json
import json, os, asyncio
from playwright.async_api import async_playwright
D = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = r"""() => {
 const c = [], add = (type, t, o={}) => c.push(Object.assign({type, t: Math.max(0, +t.toFixed(3))}, o));
 add('ding', 0.05); add('paper_rustle', 0.0, {g:.5});
 add('crayon_scribble', 0.25, {d:1.3}); add('crayon_scribble', 1.45, {d:0.9}); add('crayon_scribble', 2.2, {d:0.6, g:.6});
 add('paper_rustle', WT(1,'luna')-0.3, {g:.4}); add('paper_rustle', 3.1, {g:.4});
 add('whoosh', T12[0], {d: T12[1]-T12[0]+0.2}); add('paper_rustle', T12[0]+0.3);
 add('ding', WT(2,'luz')+0.6, {f: 1568}); add('unfold', WT(2,'esconde'), {d: 1.2});
 add('pop', WT(2,'atardecer')-0.2); add('paper_rustle', FOLD[0], {g:.6});
 add('whoosh', T23[0], {d: 0.9}); add('paper_rustle', T23[0]+0.2);
 add('slide', S3.start+0.05, {d:0.9}); 
 for (let i=0;i<8;i++) add('pop', WT(3,'bolitas')+i*0.09, {g:.35+.05*(i%3), f: 700+i*60});
 add('pop', WT(3,'moleculas'), {g:.6, f:900}); add('whoosh', T34[0], {d: 1.0}); add('paper_rustle', T34[0]+0.4);
 add('paper_rustle', S4.start+0.1, {g:.5}); add('slide_whistle', WT(4,'roja'), {d: 1.6});
 hitTimes().forEach((h,i)=>add('boing', h.t, {f: 260 + (i%4)*40}));
 add('chimes', WT(4,'vemos')-0.05, {g:.45, n:6});
 add('curtain_swish', T45[0], {d: 1.0});
 add('crayon_scribble', WT(5,'luz'), {d: 1.3, g:.5});
 add('crayon_scribble', WT(5,'azul')-0.15, {d: 0.9}); add('crayon_scribble', WT(5,'naranja')-0.15, {d: 0.9});
 const q = S5.quiz_pause; add('pop', q[0]-0.45, {g:.5});
 for (let k=0;k<4;k++) add('tick', q[0]+k*0.375, {tac: k%2});
 add('unfold', T56[0], {d: 1.0}); add('chimes', WT(6,'naranja')-0.05, {g: .8, n: 10});
 add('whoosh', WT(6,'viaje')-0.2, {d: 1.2, g: .5});
 add('sparkle', WT(6,'rebota'), {d: 1.0}); add('chimes', WT(6,'naranjas')-0.25, {g: 1.0, n: 14});
 add('sparkle', WT(6,'rojos')-0.2, {d: 1.4});
 add('curtain_swish', T67[0], {d: 1.3}); add('paper_rustle', T67[0]+0.5);
 add('ding', WT(7,'esparce'), {f: 1568, g: .6}); add('ding', WT(7,'luna')-0.1, {f: 1175, g: .6});
 add('paper_rustle', WT(7,'tema')-0.1, {g:.4}); add('ding', TL.duration-1.35, {g:.7});
 return c.sort((a,b)=>a.t-b.t);
}"""
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await b.new_page()
        await pg.goto(f"file://{D}/stage.html"); await pg.wait_for_function("window.READY === true")
        cues = await pg.evaluate(JS)
        await b.close()
    json.dump(cues, open(f"{D}/sfx_cues.json", "w"), indent=1)
    print(len(cues), "efectos;", ", ".join(f"{c['type']}@{c['t']}" for c in cues[:12]), "...")
asyncio.run(main())

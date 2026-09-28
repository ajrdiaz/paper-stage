# Lee de stage.html los tiempos de los eventos visuales y arma la lista de efectos -> sfx_cues.json
import json, os, asyncio
from playwright.async_api import async_playwright
D = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = r"""() => {
 const c = [], add = (type, t, o={}) => c.push(Object.assign({type, t: Math.max(0, +t.toFixed(3))}, o));
 // 1 · la idea
 add('paper_rustle', 0.0, {g:.5}); add('ding', 0.05);
 add('crayon_scribble', 0.25, {d:1.3}); add('crayon_scribble', 1.45, {d:0.9}); add('crayon_scribble', 2.2, {d:0.6, g:.6});
 add('pop', WT(1,'bostezo',1)-0.2, {f:800, g:.5}); add('yawn_whistle', WT(1,'bostezo',1)-0.05, {d:1.1, g:.6, f:620});
 add('pop', WT(1,'seguro')-0.1, {f:650, g:.5}); add('yawn_whistle', WT(1,'seguro')+0.15, {d:1.2, g:.55, f:480});
 add('whoosh', T12[0], {d: T12[1]-T12[0]+0.2}); add('paper_rustle', T12[0]+0.3);
 // 2 · ¿qué es un bostezo?
 add('paper_rustle', S2.start+0.1, {g:.4}); add('ding', WT(2,'bostezar'), {f:1760, g:.45});
 add('yawn_whistle', WT(2,'abrir')-0.1, {d:2.1, g:.75, f:540}); add('whoosh', WT(2,'tomar')-0.1, {d:1.0, g:.45});
 add('slide', WT(2,'como')-0.1, {d:0.8, g:.7}); add('paper_rustle', WT(2,'final')-0.2, {g:.5}); add('pop', WT(2,'quien'), {f:900, g:.6});
 add('yawn_whistle', T23[0]-0.35, {d:1.2, g:.6, f:500}); add('whoosh', T23[0]+0.2, {d:1.1}); add('paper_rustle', T23[1]-0.2, {g:.5});
 // 3 · tu cerebro copia
 add('pop', S3.start-0.3, {f:600, g:.5}); add('pop', S3.start+0.2, {f:480, g:.6});
 add('yawn_whistle', WT(3,'bostezar')-0.15, {d:1.3, g:.5, f:600});
 add('crayon_scribble', WT(3,'tu')-0.1, {d:0.9, g:.8}); add('sparkle', WT(3,'copia')-0.1, {d:0.8}); add('chimes', WT(3,'copia'), {n:5, g:.4});
 add('yawn_whistle', WT(3,'sin')-0.15, {d:1.5, g:.6, f:460});
 const tr = WT(3,'raro')-0.5; add('boing', tr, {f:260}); add('pop', tr+0.1, {f:1000, g:.4}); add('pop', tr+0.3, {f:1100, g:.4}); add('pop', tr+0.5, {f:1200, g:.4});
 add('unfold', T34[0], {d:1.1});
 // 4 · Coral y Teo
 add('paper_rustle', S4.start-0.25, {g:.5}); add('pop', WT(4,'coral')-0.1, {f:700}); add('pop', WT(4,'coral')+0.15, {f:820});
 add('yawn_whistle', WT(4,'bosteza')-0.1, {d:1.2, g:.6, f:560}); add('whoosh', WT(4,'bosteza')+0.35, {d:0.9, g:.35});
 add('yawn_whistle', WT(4,'teo')-0.05, {d:1.2, g:.6, f:660});
 add('crayon_scribble', WT(4,'conectamos')-0.35, {d:0.85, g:.8}); add('ding', WT(4,'conectamos')+0.3, {f:1760, g:.5});
 add('pop', WT(4,'familia')-0.25, {f:600}); add('pop', WT(4,'familia')-0.15, {f:760}); add('yawn_whistle', WT(4,'familia')+0.25, {d:1.1, g:.35, f:520});
 add('pop', WT(4,'amigos')-0.25, {f:680}); add('pop', WT(4,'amigos')-0.15, {f:840}); add('yawn_whistle', WT(4,'amigos')+0.25, {d:1.1, g:.35, f:600});
 for(let i=0;i<3;i++) add('pop', WT(4,'desconocidos')+0.5+i*0.15, {f:1300+i*120, g:.25});
 add('slide', T45[0], {d:1.0}); add('paper_rustle', T45[0]+0.3);
 // 5 · mini-quiz
 add('paper_rustle', S5.start+0.2, {g:.4}); add('pop', WT(5,'perrito')+0.1, {f:950, g:.6});
 add('crayon_scribble', WT(5,'si')-0.2, {d:0.8}); add('crayon_scribble', WT(5,'no')-0.1, {d:0.8});
 const q = S5.quiz_pause; add('pop', q[0]-0.45, {g:.5});
 for (let k=0;k<4;k++) add('tick', q[0]+k*0.375, {tac: k%2});
 add('unfold', T56[0], {d:1.0});
 // 6 · la revelación
 add('chimes', WT(6,'si')-0.05, {g:.9, n:12}); add('sparkle', WT(6,'si'), {d:1.2});
 add('yawn_whistle', WT(6,'bostezar')-0.1, {d:1.3, g:.5, f:620}); add('yawn_whistle', WT(6,'dueno')-0.05, {d:1.6, g:.85, f:420});
 add('chimes', WT(6,'persona')-0.1, {g:.7, n:8}); add('sparkle', WT(6,'favorita'), {d:1.0});
 add('whoosh', WT(6,'chimpances')-0.45, {d:0.6, g:.5}); add('boing', WT(6,'chimpances')+0.1, {f:220});
 add('yawn_whistle', WT(6,'tambien')-0.25, {d:1.3, g:.6, f:380}); add('chimes', WT(6,'tambien')+0.35, {g:1.0, n:14});
 add('curtain_swish', T67[0], {d:1.3}); add('paper_rustle', T67[0]+0.5);
 // 7 · de vuelta al escenario
 add('yawn_whistle', WT(7,'copia')-0.15, {d:1.1, g:.35, f:620}); add('yawn_whistle', WT(7,'bostezos')+0.05, {d:1.2, g:.35, f:480});
 add('crayon_scribble', WT(7,'hilito')-0.2, {d:1.0, g:.8}); add('ding', WT(7,'une'), {f:1760, g:.5});
 add('ding', WT(7,'sabes')-0.1, {g:.7}); add('sparkle', WT(7,'sabes'), {d:0.9});
 add('paper_rustle', WT(7,'sigueme')-0.1, {g:.4}); add('ding', TL.duration-1.35, {g:.6});
 return c.sort((a,b)=>a.t-b.t);
}"""
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await b.new_page()
        await pg.goto(f"file://{D}/stage.html"); await pg.wait_for_function("window.READY === true")
        cues = await pg.evaluate(JS)
        await b.close()
    json.dump(cues, open(f"{D}/sfx_cues.json", "w"), indent=1)
    print(len(cues), "efectos;", ", ".join(f"{c['type']}@{c['t']}" for c in cues[:10]), "...")
asyncio.run(main())

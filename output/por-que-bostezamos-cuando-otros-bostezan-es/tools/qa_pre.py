# Comprobaciones automáticas del QA previo (sin final.mp4)
import json, subprocess, re, os
import numpy as np, soundfile as sf
D = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tl = json.load(open(f"{D}/timeline.json"))
tot = sum(s["end"]-s["start"] for s in tl["scenes"])
print(f"timeline: duración {tl['duration']:.3f} s | suma de escenas {tot:.3f} s | {'OK' if 60 <= tot <= 65 else 'FALLA'}")
v, sr = sf.read(f"{D}/voice.wav")
thr = 10**(-40/20); idx = np.where(np.abs(v) > thr)[0]; print("voz empieza en:", round(idx[0]/sr, 3), "s")
for s in tl["scenes"]:
    a, b = int(s["voice_start"]*sr), int(min(s["end"], tl["duration"])*sr); seg = np.where(np.abs(v[a:b]) > thr)[0]
    real_end = (a + seg[-1])/sr
    # ¿hay voz de esta escena más allá de su final? (la WAV de la escena completa, colocada en voice_start)
    sc, _ = sf.read(f"{D}/audio/scene_{s['n']:02d}.wav"); last = np.where(np.abs(sc) > thr)[0][-1]/sr + s["voice_start"]
    print(f"escena {s['n']}: {s['start']:6.2f}-{s['end']:6.2f}  voz {s['voice_start']:6.2f}-{last:6.2f}  margen {s['end']-last:.2f} s  {'OK' if last < s['end'] else 'FALLA'}")
m = np.array(json.load(open(f"{D}/mouth.json"))["v"])
hop = sr//24; act = np.array([np.sqrt(np.mean(v[i*hop:(i+1)*hop]**2)) if i*hop < len(v) else 0 for i in range(len(m))]) > thr
near = np.convolve(act, [1,1,1], "same").astype(bool)
print("lip-sync: frames", len(m), "| boca abierta (>0.05) sin voz:", int(np.sum((m > 0.05) & ~near)), "| con voz y boca cerrada (<0.02):", int(np.sum((m < 0.02) & act)), "de", int(act.sum()), "con voz")
def lufs(p):
    r = subprocess.run(["ffmpeg","-hide_banner","-i",p,"-af","loudnorm=print_format=json","-f","null","-"],capture_output=True,text=True).stderr
    return json.loads(re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r, re.S).group(0))
j = lufs(f"{D}/mix.wav"); print("mix.wav:", j["input_i"], "LUFS, TP", j["input_tp"], "dBTP")
S = json.load(open(f"{D}/script.json")); print("palabras de narración:", sum(len(e["vo"].split()) for e in S["escenas"]))
print("frase más larga (palabras):", max(len(x.split()) for e in S["escenas"] for x in re.split(r"(?<=[.!?…])\s+", e["vo"])))

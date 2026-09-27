# Genera una WAV por escena con Kokoro (ef_dora): cada frase por separado, recortada y unida con
# una "pausa de asombro" de GAP s; luego tono 1.05x (rubberband) y 0.30 s de silencio final.
import json, subprocess, sys, os
import numpy as np, soundfile as sf
from kokoro import KPipeline
D = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = json.load(open(f"{D}/script.json"))
speed = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
GAP = float(os.environ.get("GAP", "0.38"))
only = [int(x) for x in sys.argv[2:]]
pipe = KPipeline(lang_code="e", repo_id="hexgrad/Kokoro-82M")
SR = 24000
def trim(a, thr=10**(-45/20)):
    idx = np.where(np.abs(a) > thr)[0]
    if not len(idx): return a
    s = max(0, idx[0] - int(0.01*SR)); e = min(len(a), idx[-1] + int(0.06*SR))
    return a[s:e]
def dur(p): return float(subprocess.check_output(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",p]).decode())
out = {}
for sc in S["escenas"]:
    n = sc["n"]
    if only and n not in only: continue
    parts = []
    for line in sc.get("vo_tts", sc["vo"]).split("\n"):
        chunks = [np.asarray(a) for _, _, a in pipe(line, voice="ef_dora", speed=speed, split_pattern=None)]
        parts.append(trim(np.concatenate(chunks)))
    gap = np.zeros(int(GAP*SR), dtype=np.float32)
    audio = parts[0]
    for p in parts[1:]: audio = np.concatenate([audio, gap, p])
    trimf = f"{D}/audio/trim_{n:02d}.wav"; pit = f"{D}/audio/pitch_{n:02d}.wav"; dst = f"{D}/audio/scene_{n:02d}.wav"
    sf.write(f"{D}/audio/raw_{n:02d}.wav", audio, SR)
    subprocess.run(["ffmpeg","-y","-loglevel","error","-i",f"{D}/audio/raw_{n:02d}.wav","-ar","48000","-ac","1",trimf],check=True)
    subprocess.run(["rubberband","-q","--fine","-p","0.845",trimf,pit],check=True,stderr=subprocess.DEVNULL)
    subprocess.run(["ffmpeg","-y","-loglevel","error","-i",pit,"-af","apad=pad_dur=0.30","-ac","1","-ar","48000",dst],check=True)
    out[n] = dur(dst)
    print(n, round(out[n],2), len(sc["vo"].split()), "palabras", flush=True)
print("TOTAL", round(sum(out.values()),2), "palabras", sum(len(s["vo"].split()) for s in S["escenas"]))

# Comprobaciones automáticas del QA
import json, subprocess, re, os
import numpy as np, soundfile as sf
D = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def probe(args): return subprocess.check_output(["ffprobe","-v","error"]+args+[f"{D}/final.mp4"]).decode().strip()
print("formato:", probe(["-show_entries","format=duration","-of","csv=p=0"]))
print("video:", probe(["-select_streams","v:0","-show_entries","stream=codec_name,width,height,r_frame_rate,pix_fmt,nb_frames","-of","csv=p=0"]))
print("audio:", probe(["-select_streams","a:0","-show_entries","stream=codec_name,sample_rate,channels,duration","-of","csv=p=0"]))
r = subprocess.run(["ffmpeg","-hide_banner","-i",f"{D}/final.mp4","-vn","-af","loudnorm=print_format=json","-f","null","-"],capture_output=True,text=True).stderr
j = json.loads(re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r, re.S).group(0)); print("LUFS final:", j["input_i"], "TP:", j["input_tp"])
tl = json.load(open(f"{D}/timeline.json"))
v, sr = sf.read(f"{D}/voice.wav")
idx = np.where(np.abs(v) > 10**(-40/20))[0]; print("voz empieza en:", round(idx[0]/sr, 3), "s")
for s in tl["scenes"]:
    # fin real de la voz de la escena (último sample > -40 dB dentro de su tramo)
    a, b = int(s["voice_start"]*sr), int(s["voice_end"]*sr); seg = np.where(np.abs(v[a:b]) > 10**(-40/20))[0]
    real_end = (a + seg[-1])/sr
    print(f"escena {s['n']}: {s['start']:.2f}-{s['end']:.2f}  voz {s['voice_start']:.2f}-{real_end:.2f}  margen {s['end']-real_end:.2f} s  {'OK' if real_end < s['end'] else 'FALLA'}")
m = np.array(json.load(open(f"{D}/mouth.json"))["v"])
hop = sr//24; act = np.array([np.sqrt(np.mean(v[i*hop:(i+1)*hop]**2)) for i in range(len(m))]) > 10**(-40/20)
print("lip-sync: frames boca abierta sin voz:", int(np.sum((m > 0.05) & ~np.convolve(act, [1,1,1], "same").astype(bool))), "| frames con voz y boca cerrada:", int(np.sum((m < 0.02) & act)), "de", int(act.sum()))

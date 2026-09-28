# Mide la música tras el ducking (misma cadena que mix.py) frente a la voz, con y sin narración
import json, subprocess, re, os
import numpy as np, soundfile as sf
D = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(f"{D}/tools/mix.py").read()
gm = None
def lufs(p):
    r = subprocess.run(["ffmpeg","-hide_banner","-i",p,"-af","loudnorm=print_format=json","-f","null","-"],capture_output=True,text=True).stderr
    return float(json.loads(re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r, re.S).group(0))["input_i"])
Lv, Lm = lufs(f"{D}/voice.wav"), lufs(f"{D}/music.wav"); gm = (Lv-12)-Lm+5.5
fc = (f"[0:a]aformat=channel_layouts=stereo,asetpts=PTS[vsc];[1:a]volume={gm:.2f}dB[m];[m][vsc]sidechaincompress=threshold=0.015:ratio=8:attack=25:release=450:knee=4:makeup=1[md]")
subprocess.run(["ffmpeg","-y","-loglevel","error","-i",f"{D}/voice.wav","-i",f"{D}/music.wav","-filter_complex",fc,"-map","[md]","-ar","48000",f"{D}/audio/music_ducked.wav"],check=True)
v,_ = sf.read(f"{D}/voice.wav"); m,_ = sf.read(f"{D}/audio/music_ducked.wav"); m = m.mean(axis=1) if m.ndim>1 else m
n = min(len(v),len(m)); hop = 4800
rv = np.array([np.sqrt(np.mean(v[i:i+hop]**2)) for i in range(0,n-hop,hop)]); rm = np.array([np.sqrt(np.mean(m[i:i+hop]**2)) for i in range(0,n-hop,hop)])
talk = rv > 10**(-35/20); db = lambda x: 20*np.log10(x+1e-9)
vref = db(np.sqrt(np.mean(rv[talk]**2)))
print(f"música bajo la voz: {db(np.sqrt(np.mean(rm[talk]**2)))-vref:.1f} dB respecto a la voz")
print(f"música en huecos:   {db(np.sqrt(np.mean(rm[~talk]**2)))-vref:.1f} dB respecto a la voz")

# Mezcla voz + música (ducking por sidechain) + efectos -> mix.wav a -14 LUFS (loudnorm en 2 pasadas)
import json, subprocess, os, re
D = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def lufs(path, af=""):
    r = subprocess.run(["ffmpeg","-hide_banner","-i",path,"-af",(af+"," if af else "")+"loudnorm=print_format=json","-f","null","-"],capture_output=True,text=True).stderr
    return json.loads(re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r, re.S).group(0))
Lv = float(lufs(f"{D}/voice.wav")["input_i"]); Lm = float(lufs(f"{D}/music.wav")["input_i"]); Ls = float(lufs(f"{D}/sfx.wav")["input_i"])
gm = (Lv - 12) - Lm + 5.5  # música: calibrado con tools/duck_check.py -> ~-12 dB de la voz en los huecos y ~-20 dB bajo la voz (ducking)
gs = (Lv - 10) - Ls      # efectos a -10 dB
print(f"voz {Lv:.1f} LUFS | música {Lm:.1f} -> {gm:+.1f} dB | efectos {Ls:.1f} -> {gs:+.1f} dB")
fc = (f"[0:a]aformat=channel_layouts=stereo,asplit=2[v][vsc];"
      f"[1:a]volume={gm:.2f}dB[m];[m][vsc]sidechaincompress=threshold=0.015:ratio=8:attack=25:release=450:knee=4:makeup=1[md];"
      f"[2:a]volume={gs:.2f}dB[s];[v][md][s]amix=inputs=3:normalize=0:duration=first[pre]")
pre = f"{D}/audio/premix.wav"
subprocess.run(["ffmpeg","-y","-loglevel","error","-i",f"{D}/voice.wav","-i",f"{D}/music.wav","-i",f"{D}/sfx.wav","-filter_complex",fc,"-map","[pre]","-ar","48000",pre],check=True)
m = lufs(pre)
ln = (f"loudnorm=I=-14:TP=-1.5:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:"
      f"measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
subprocess.run(["ffmpeg","-y","-loglevel","error","-i",pre,"-af",ln,"-ar","48000","-c:a","pcm_s16le",f"{D}/mix.wav"],check=True)
f = lufs(f"{D}/mix.wav"); print("mix.wav:", f["input_i"], "LUFS, TP", f["input_tp"])

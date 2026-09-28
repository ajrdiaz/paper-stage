# timeline.json + voice.wav + words.json + subtitles.srt + mouth.json
import json, subprocess, re, unicodedata, difflib, os
import numpy as np, soundfile as sf
D = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = json.load(open(f"{D}/script.json"))
SR = 48000; FPS = 24
LEAD = {1: 0.12}; BREATH = 0.50; QUIZ_PAUSE = 1.5
scenes = []; t = 0.0
for sc in S["escenas"]:
    n = sc["n"]; a, sr = sf.read(f"{D}/audio/scene_{n:02d}.wav"); assert sr == SR
    lead = LEAD.get(n, 0.10); ad = len(a) / SR
    extra = QUIZ_PAUSE if n == 5 else 0.0
    dur = lead + ad + BREATH + extra
    scenes.append(dict(n=n, titulo=sc["titulo"], start=round(t, 4), end=round(t + dur, 4),
                       voice_start=round(t + lead, 4), voice_end=round(t + lead + ad, 4), audio_dur=round(ad, 4),
                       quiz_pause=[round(t + lead + ad, 4), round(t + lead + ad + extra, 4)] if extra else None))
    t += dur
total = round(t, 4)
voice = np.zeros(int(np.ceil(total * SR)) + SR // 10, dtype=np.float32)
for s in scenes:
    a, _ = sf.read(f"{D}/audio/scene_{s['n']:02d}.wav", dtype="float32")
    i = int(round(s["voice_start"] * SR)); voice[i:i + len(a)] += a
voice = voice[:int(np.ceil(total * SR))]
sf.write(f"{D}/voice.wav", voice, SR)

# --- words (faster-whisper small) aligned to script orthography ---
from faster_whisper import WhisperModel
model = WhisperModel("small", device="cpu", compute_type="int8")
def norm(w): return re.sub(r"[^a-z0-9ñ]", "", unicodedata.normalize("NFD", w.lower()).encode("ascii", "ignore").decode()) or w
words = []; report = []
for s, sc in zip(scenes, S["escenas"]):
    segs, _ = model.transcribe(f"{D}/audio/scene_{s['n']:02d}.wav", language="es", word_timestamps=True,
                               initial_prompt=sc["vo"], vad_filter=False, beam_size=5)
    ww = [w for seg in segs for w in seg.words]
    heard = " ".join(w.word.strip() for w in ww)
    report.append(f"Escena {s['n']}: {heard}")
    ref = sc["vo"].split()
    A = [norm(w) for w in ref]; B = [norm(w.word) for w in ww]
    times = [None] * len(ref)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, A, B, autojunk=False).get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            for k in range(i2 - i1): times[i1 + k] = (ww[j1 + k].start, ww[j1 + k].end)
        elif tag == "replace":
            st, en = ww[j1].start, ww[j2 - 1].end; step = (en - st) / (i2 - i1)
            for k in range(i2 - i1): times[i1 + k] = (st + k * step, st + (k + 1) * step)
    # interpolate leftovers
    for i in range(len(ref)):
        if times[i] is None:
            prev = next((times[j][1] for j in range(i - 1, -1, -1) if times[j]), 0.0)
            nxt = next((times[j][0] for j in range(i + 1, len(ref)) if times[j]), prev + 0.3)
            times[i] = (prev, max(prev + 0.1, min(nxt, prev + 0.4)))
    for w, (st, en) in zip(ref, times):
        words.append(dict(w=w, s=round(s["voice_start"] + st, 3), e=round(s["voice_start"] + en, 3), scene=s["n"]))
json.dump(words, open(f"{D}/words.json", "w"), ensure_ascii=False, indent=1)
open(f"{D}/audio/whisper_report.txt", "w").write("\n".join(report))

# --- SRT: caption chunks (<= 2 lines, ~ 7 words) broken at punctuation ---
def chunks(ws, MAX=40):
    L = lambda c: len(" ".join(x["w"] for x in c))
    phrases, cur = [], []
    for w in ws:
        if cur and cur[-1]["scene"] != w["scene"]: phrases.append(cur); cur = []
        cur.append(w)
        if re.search(r"[.,?!…:;]$", w["w"]): phrases.append(cur); cur = []
    if cur: phrases.append(cur)
    def split(p):
        if L(p) <= MAX or len(p) < 2: return [p]
        k = min(range(1, len(p)), key=lambda k: max(L(p[:k]), L(p[k:])))
        return split(p[:k]) + split(p[k:])
    ph = [q for p in phrases for q in split(p)]
    out = []
    for p in ph:
        if out and out[-1][-1]["scene"] == p[0]["scene"] and L(out[-1] + p) <= MAX and p[0]["s"] - out[-1][-1]["e"] < 0.7:
            out[-1] = out[-1] + p
        else: out.append(p)
    return out
caps = chunks(words)
def ts(x): h = int(x // 3600); m = int(x % 3600 // 60); s = x % 60; return f"{h:02d}:{m:02d}:{int(s):02d},{int(round((s % 1) * 1000)) % 1000:03d}"
with open(f"{D}/subtitles.srt", "w") as f:
    for i, c in enumerate(caps, 1):
        end = c[-1]["e"] + 0.25
        if i < len(caps): end = min(end, caps[i][0]["s"])
        f.write(f"{i}\n{ts(c[0]['s'])} --> {ts(end)}\n{' '.join(x['w'] for x in c)}\n\n")
captions = [dict(s=c[0]["s"], e=c[-1]["e"], i0=words.index(c[0]), i1=words.index(c[-1])) for c in caps]

# --- mouth.json: RMS envelope at 24 fps, 0..1 ---
hop = SR // FPS; nfr = int(np.ceil(total * FPS))
env = np.array([np.sqrt(np.mean(voice[i * hop:(i + 1) * hop + hop // 2] ** 2) + 1e-12) for i in range(nfr)])
db = 20 * np.log10(env + 1e-9)
m = np.clip((db + 42) / 26, 0, 1)            # -42 dB -> 0, -16 dB -> 1
m[db < -40] = 0
m = np.round(np.convolve(m, [0.25, 0.5, 0.25], mode="same"), 3)
# compuerta: boca cerrada si no hay voz (> -40 dB) en el frame ni en sus vecinos
act = np.array([np.sqrt(np.mean(voice[i*hop:(i+1)*hop]**2)) if i*hop < len(voice) else 0 for i in range(nfr)]) > 10**(-40/20)
m[~np.convolve(act, [1, 1, 1], "same").astype(bool)] = 0
json.dump(dict(fps=FPS, v=m.tolist()), open(f"{D}/mouth.json", "w"))
json.dump(dict(duration=total, fps=FPS, scenes=scenes, captions=captions), open(f"{D}/timeline.json", "w"), ensure_ascii=False, indent=1)
print("TOTAL", total)
for s in scenes: print(s["n"], s["start"], s["end"], "voz", s["voice_start"], "-", s["voice_end"])
print("\n".join(report))

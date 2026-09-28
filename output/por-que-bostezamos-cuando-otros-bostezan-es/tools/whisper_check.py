# faster-whisper small SIN prompt sobre voice.wav; compara tiempos con words.json
import json, re, unicodedata, os, sys
from faster_whisper import WhisperModel
D = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
norm = lambda w: re.sub(r"[^a-z0-9ñ]", "", unicodedata.normalize("NFD", w.lower()).encode("ascii","ignore").decode())
m = WhisperModel("small", device="cpu", compute_type="int8")
segs, _ = m.transcribe(f"{D}/voice.wav", language="es", word_timestamps=True, vad_filter=False, beam_size=5)
ww = [w for s in segs for w in s.words]
print("TRANSCRIPCIÓN:", " ".join(w.word.strip() for w in ww))
W = json.load(open(f"{D}/words.json"))
targets = sys.argv[1:] or ["gatito", "cerebro", "desconocidos", "perrito", "chimpancés", "aprender"]
out = []
for tg in targets:
    a = next((x for x in W if norm(x["w"]) == norm(tg)), None)
    b = min((w for w in ww if norm(w.word) == norm(tg)), key=lambda w: abs(w.start - a["s"]) if a else 0, default=None)
    if a and b: out.append((tg, a["s"], round(b.start, 3), round(abs(a["s"] - b.start) * 1000)))
    else: out.append((tg, a and a["s"], None, None))
for o in out: print(f"{o[0]:14s} words.json {o[1]}  whisper {o[2]}  desfase {o[3]} ms")
json.dump(dict(text=" ".join(w.word.strip() for w in ww), checks=out), open(f"{D}/audio/whisper_noprompt.json", "w"), ensure_ascii=False, indent=1)

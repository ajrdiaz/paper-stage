# Une _head.html (marco del teatrito y Lía) + _body.html (escenas del tema) -> stage_src.html
import os
D = os.path.dirname(os.path.abspath(__file__))
h = open(f"{D}/_head.html").read(); b = open(f"{D}/_body.html").read()
h = h.replace("Teatrito de Papel — ¿Por qué el cielo es azul?", "Teatrito de Papel — ¿Por qué bostezamos cuando otros bostezan?")
old = "const T12=[S1.end-1.0, S1.end+0.3], T23=[S2.end-0.45, S2.end+0.45], FOLD=[S2.end-0.95, S2.end-0.4];\nconst T34=[S3.end-0.75, S3.end+0.35], T45=[S4.end-0.55, S4.end+0.5], T56=[S5.end-0.55, S5.end+0.45], T67=[S6.end-0.5, S6.end+0.8];"
assert old in h
h = h.replace(old, "const T12=[S1.end-1.0, S1.end+0.3], T23=[S2.end-0.85, S2.end+0.4];\nconst T34=[S3.end-0.7, S3.end+0.4], T45=[S4.end-0.55, S4.end+0.5], T56=[S5.end-0.55, S5.end+0.45], T67=[S6.end-0.5, S6.end+0.8];")
h = h.replace("</defs>", '''  <linearGradient id="duskP" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#A996D6"/><stop offset="0.5" stop-color="#D7B6D9"/><stop offset="0.85" stop-color="#F6C8A8"/><stop offset="1" stop-color="#F7B98E"/></linearGradient>
  <linearGradient id="goldP" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#FBE7A6"/><stop offset="0.6" stop-color="#FAD58E"/><stop offset="1" stop-color="#F8C28A"/></linearGradient>
</defs>''', 1)
open(f"{D}/stage_src.html", "w").write(h + b)

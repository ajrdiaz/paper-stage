# Construye stage.html autocontenido: fuentes en base64 + timeline/words/mouth incrustados.
import base64, json, os
D = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(f"{D}/tools/stage_src.html").read()
def ff(name, file, weight):
    b = base64.b64encode(open(f"{D}/assets/fonts/{file}", "rb").read()).decode()
    return f"@font-face{{font-family:'{name}';src:url(data:font/ttf;base64,{b}) format('truetype');font-weight:{weight};font-style:normal}}"
fonts = ff("Gaegu", "Gaegu-Bold.ttf", "700") + "\n" + ff("Fredoka", "Fredoka-Variable.ttf", "300 700")
data = dict(timeline=json.load(open(f"{D}/timeline.json")), words=json.load(open(f"{D}/words.json")), mouth=json.load(open(f"{D}/mouth.json")))
out = src.replace("/*__FONTS__*/", fonts).replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
open(f"{D}/stage.html", "w").write(out)
print("stage.html", len(out)//1024, "KB")

# Hoja de contacto: python tools/sheet.py out.png snaps/a.png snaps/b.png ...  (6 por fila, recorte opcional)
import sys
from PIL import Image, ImageDraw
out, files = sys.argv[1], sys.argv[2:]
W, H = 360, 640; cols = 6; rows = (len(files)+cols-1)//cols
sheet = Image.new("RGB", (cols*W, rows*(H+30)), "white"); d = ImageDraw.Draw(sheet)
for i, p in enumerate(files):
    im = Image.open(p).convert("RGB").resize((W, H)); x, y = (i%cols)*W, (i//cols)*(H+30)
    sheet.paste(im, (x, y+30)); d.text((x+8, y+8), p.split("/")[-1], fill="black")
sheet.save(out)

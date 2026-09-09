"""Génère assets/alfred.ico : un monogramme "A" élégant, thème majordome
(noir/or), utilisé comme icône de l'exécutable et de la barre des tâches."""

from PIL import Image, ImageDraw, ImageFont
import os

SIZE = 256
BG = (20, 20, 24, 255)
GOLD = (198, 161, 91, 255)

img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

draw.ellipse((4, 4, SIZE - 4, SIZE - 4), fill=BG, outline=GOLD, width=6)

font = None
for candidate in (
    r"C:\Windows\Fonts\georgia.ttf",
    r"C:\Windows\Fonts\times.ttf",
    r"C:\Windows\Fonts\arial.ttf",
):
    if os.path.exists(candidate):
        font = ImageFont.truetype(candidate, 140)
        break
if font is None:
    font = ImageFont.load_default()

text = "A"
bbox = draw.textbbox((0, 0), text, font=font)
w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
draw.text(((SIZE - w) / 2 - bbox[0], (SIZE - h) / 2 - bbox[1]), text, font=font, fill=GOLD)

out_dir = os.path.dirname(os.path.abspath(__file__))
img.save(os.path.join(out_dir, "alfred.ico"), sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.save(os.path.join(out_dir, "alfred.png"))
print("Icône générée")

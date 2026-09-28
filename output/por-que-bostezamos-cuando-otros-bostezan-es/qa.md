# QA final — «¿Por qué bostezamos cuando otros bostezan?» (es · vertical)

Comprobado sobre `final.mp4` con `ffprobe`/`ffmpeg` (`tools/qa.py`, `blackdetect`), revisión visual de frames extraídos del MP4 y validación de `publish.json`. El QA previo (tiempos, sincronía, contenido y fotogramas de `stage.html`) está en `qa_previo.md` y se cumplió entero antes del render.

- [x] **Duración entre 60.0 y 65.0 s, y audio y video duran lo mismo (±0.1 s):** contenedor **63.25 s**; video 1518 frames = 63.250 s; audio AAC 63.232 s → diferencia **0.018 s**.
- [x] **Formato técnico:** 1080×1920, **24 fps** (24/1), **H.264 `yuv420p`** (rango TV, BT.709), **AAC 48 kHz** estéreo 192 kb/s. Sonoridad **−14.35 LUFS** integrados (objetivo −14 ±1), true peak −1.40 dBTP.
  - *Corregido:* la primera codificación salió como `yuvj420p` (rango completo heredado de los JPG capturados). Se volvió a codificar con `scale=in_range=pc:out_range=tv,format=yuv420p` sin recapturar frames.
- [x] **Revisión visual del MP4** (un frame a mitad de cada escena: 3.82 · 12.04 · 20.11 · 29.71 · 40.03 · 48.75 · 58.15 s, en `snaps/qa_final.png`, y uno a tamaño completo): se ven igual que en el QA previo (título en crayón, Mishi estirándose, el cerebro que bosteza, Coral y Teo con el hilo y el corazón, el quiz SÍ/NO con Canelo, Canelo bostezando, el recap en la pared amarilla). Sin artefactos de compresión visibles (CRF 18, preset slow); `blackdetect` no encontró ningún tramo negro.
- [x] **`publish.json`:** exactamente **5 hashtags**; `descripcion_corta` de **118** caracteres (≤150, sin hashtags ni fuentes); títulos de **35, 38 y 44** caracteres (≤60); `texto_portada` «¿Se contagian los bostezos?» (4 palabras, 1 línea); `frame_portada_s` = 3.0 s (escena 1 completa, título y subrayado ya dibujados, Lía visible); `hecho_para_ninos: true`; los subtítulos están en el elemento `#subs`.

- [x] **Final de la música (corregido tras la revisión):** el acorde final de caja musical (Fa, 62.16 s) quedaba tapado por un compás nuevo en Si♭ que empezaba en 62.4 s, así que el video terminaba en IV. En `audio.html` ya no se empiezan compases encima del acorde final y el último compás va en V (Do con Si♭), para cerrar V7 → I. Solo cambió `music.wav` desde 60.0 s; se rehízo `mix.wav` (−14.35 LUFS, TP −1.49; música −19.4 dB bajo la voz y −12.4 dB en los huecos) y se remultiplexó el audio en `final.mp4` sin recodificar el video.

## Resumen de sincronía (del QA previo, sin cambios tras el render)
- Voz desde **0.18 s**; ninguna línea se pasa de su escena (margen mínimo 0.86 s; 2.36 s en el quiz por la pausa de 1.5 s con tic-tac).
- Lip-sync: 0 frames con la boca abierta sin voz y 0 frames con voz y la boca cerrada.
- Subtítulos: desfase whisper sin prompt vs `words.json` de 1–40 ms en 6 puntos.

## Observaciones menores (no bloquean)
- Whisper sin prompt oye «Tilo en voz alta» en el archivo largo; la WAV de la escena transcrita sola da «Dilo en voz alta». Los subtítulos muestran el texto correcto.
- La franja de subtítulos (y≈1466–1694) entra en parte en los 400 px inferiores; es la posición que pide el formato (§4: subtítulos en y≈1500–1680) y el texto queda por encima de la zona de botones de las apps.

## Seguridad y contenido
Tema apto para 6–9 años (§9): cotidiano, sin miedo, burlas ni consumo. Ciencia sin exagerar: se evita el mito del oxígeno y la relación con la empatía se presenta como «los científicos creen». Personajes originales (Lía, Mishi, Coral, Teo, Canelo); música y efectos sintetizados por código; voz Kokoro local. CTA exacto: «¡Sígueme para aprender más!», sin pedir comentarios ni datos a los niños. `hecho_para_ninos: true`.

**Resultado: todos los puntos del QA se cumplen.**

# QA — «¿Por qué el cielo es azul?» (es · vertical)

Comprobado sobre `final.mp4` con `ffprobe`/`ffmpeg`, `tools/qa.py`, faster-whisper `small` sin prompt y revisión visual de los frames.

- [x] **Duración entre 60.0 y 65.0 s:** 61.79 s (formato), vídeo 1483 frames, audio 61.78 s.
- [x] **Formato técnico:** 1080×1920, 24 fps, H.264 `yuv420p` (rango TV), AAC 48 kHz estéreo 192 kb/s. Sonoridad **−14.54 LUFS** (objetivo −14 ±1), true peak −1.27 dBTP.
- [x] **Arranque:** la voz empieza en **0.129 s** (≤0.3 s). El frame 1 ya tiene movimiento: líneas de idea que pulsan, boil del papel, deriva de cámara y el título empieza a dibujarse en 0.25 s.
- [x] **Ninguna línea se pasa de su escena:** el margen entre el fin real de la voz y el fin de la escena es de 0.91–0.94 s (2.42 s en la escena 5, por la pausa del quiz de 1.5 s).

  | Escena | Tramo | Voz | Margen |
  |---|---|---|---|
  | 1 La idea | 0.00–5.64 | 0.12–4.73 | 0.91 s |
  | 2 La luz del sol | 5.64–14.18 | 5.74–13.25 | 0.93 s |
  | 3 Las bolitas | 14.18–21.18 | 14.28–20.25 | 0.94 s |
  | 4 Rojo y Azul | 21.18–33.43 | 21.28–32.52 | 0.91 s |
  | 5 Mini-quiz | 33.43–43.44 | 33.53–41.02 (+1.5 s de tic-tac) | 2.42 s |
  | 6 El atardecer | 43.44–51.73 | 43.54–50.81 | 0.91 s |
  | 7 De vuelta | 51.73–61.80 | 51.83–60.89 | 0.91 s |

- [x] **Revisión visual** (21 frames con `fps=1/3` + primer y último frame, todos revisados): estilo de papel rasgado, gouache y crayón consistente; Lía igual en todas las tomas (bob negro, gancho amarillo, suéter teal con cuello blanco, crayón azul); nada importante en los 230 px superiores ni en los 120 px de la derecha; subtítulos siempre dentro de la franja marina (y≈1466–1694), en 1–2 líneas y sin cortes; ningún frame vacío. *Corregido antes del render:* el título en crayón se salía por detrás de las cortinas (se redujo de 100 a 80 px) y las opciones del quiz chocaban con las nubes (se bajaron 70 px).
- [x] **Lip-sync:** 0 frames con la boca abierta sin voz y 0 frames con voz y la boca cerrada (de 958 con voz). *Corregido:* había 13 frames de anticipación, 1–2 frames antes de cada frase (boca 0.07–0.17); se pusieron a 0 en `mouth.json` y se volvieron a renderizar solo esos frames.
- [x] **Subtítulos sincronizados** (whisper sin prompt contra `words.json`): «luz» 19 ms · «naranja» 1 ms · «esparce» 6 ms, todos por debajo de 100 ms. En «imagínalo» whisper sin prompt da 1084 ms de error, pero es un fallo de su marca de tiempo al empezar un segmento: la señal está en silencio (−107 dB) entre 20.3 y 21.2 s y la voz empieza en 21.28 s, justo donde lo marca `words.json`. La ortografía de los subtítulos sale del guion (tildes, ¿¡, «…»).
- [x] **Cada dato está respaldado en `research.md`:** cielo negro en la Luna (#1), luz blanca = todos los colores (#2), moléculas del aire (#3), el azul se dispersa más que el rojo, contado como «imagínalo así» (#4), atardecer por el camino más largo (#5).
- [x] **El bucle abierto se cierra:** en la escena 2, «Al final verás por qué el atardecer se pone naranja» (con un solecito naranja que asoma); se prepara en el quiz de la escena 5 y se resuelve en la escena 6: «¡Naranja! … Solo llegan los naranjas y rojos».
- [x] **Loop:** el primer y el último frame son el mismo escenario amarillo con los dos dibujos, Lía en la misma pose (crayón hacia el cielo azul) y las líneas de idea. La última frase vuelve a la Luna, igual que el gancho.
- [x] **Todo en `es`:** voz Kokoro `ef_dora` (español neutro, velocidad 0.98, tono ×1.05 con rubberband), 155 palabras, título, opciones del quiz («AZUL», «NARANJA»), subtítulos y metadatos en español. El concepto clave se dice dos veces con palabras distintas (escenas 4 y 7).

## Observaciones menores (no bloquean)
- Whisper sin prompt oye «tilo» en «¡Dilo en voz alta!» y «a un» en «aun». Al escucharlo se entiende «dilo», porque la /d/ inicial de Kokoro es suave. Los subtítulos muestran el texto correcto.
- En la escena 5 los subtítulos se quedan vacíos durante la pausa del quiz, a propósito, para que los niños respondan.

## Seguridad y contenido
Tema apto para 6–9 años (§9). Sin miedo, burlas ni consumo. Personajes originales, música y efectos sintetizados por código, sin material con copyright. El CTA es para los adultos («¿Qué tema quieren mañana? Escríbanlo en los comentarios») y no pide datos a los niños. `publish.json` → `hecho_para_ninos: true`.

**Resultado: todos los puntos del QA se cumplen.**

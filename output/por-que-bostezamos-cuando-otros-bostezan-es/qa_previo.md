# QA previo — «¿Por qué bostezamos cuando otros bostezan?» (es · vertical)

Comprobado antes del render con `tools/qa_pre.py`, `tools/whisper_check.py` (faster-whisper `small` **sin prompt** sobre `voice.wav`), `tools/duck_check.py` y revisión visual de `stage.html` con `renderAt` (`tools/snap.py`).

- [x] **La suma de escenas de `timeline.json` da entre 60.0 y 65.0 s:** 63.264 s (duración = suma de las 7 escenas).
- [x] **La voz empieza en ≤0.3 s y ninguna línea se pasa de su escena:** la voz empieza en **0.18 s** (`voice.wav`, primer sample > −40 dB). Margen entre el fin real de cada WAV y el fin de su escena:

  | Escena | Tramo (s) | Voz (s) | Margen |
  |---|---|---|---|
  | 1 La idea | 0.00–7.65 | 0.12–6.79 | 0.86 s |
  | 2 ¿Qué es un bostezo? | 7.65–16.43 | 7.75–15.56 | 0.87 s |
  | 3 Tu cerebro copia | 16.43–23.80 | 16.53–22.91 | 0.89 s |
  | 4 Coral y Teo | 23.80–35.61 | 23.90–34.73 | 0.87 s |
  | 5 Mini-quiz | 35.61–44.46 | 35.71–42.10 (+1.5 s de tic-tac en 42.46–43.96) | 2.36 s |
  | 6 ¡Sí se contagia! | 44.46–53.04 | 44.56–52.17 | 0.87 s |
  | 7 De vuelta al escenario | 53.04–63.26 | 53.14–62.40 | 0.86 s |

  Voz: Kokoro `ef_dora`, velocidad 0.98, tono ×1.05 (rubberband, +0.845 semitonos), sin acelerar. 154 palabras; la frase más larga tiene 14 palabras.
- [x] **Lip-sync (`mouth.json`):** 1519 frames; **0** frames con la boca abierta (>0.05) sin voz a ±1 frame y **0** frames con voz y la boca cerrada (de 990 con voz). *Corregido:* la primera versión tenía 9 frames de "cola" por el suavizado; se añadió una compuerta en `tools/sync.py`.
- [x] **Subtítulos sincronizados** (whisper sin prompt vs `words.json`): «gatito» 12 ms · «cerebro» 29 ms · «desconocidos» 40 ms · «perrito» 14 ms · «chimpancés» 1 ms · «aprender» 3 ms (todos < 100 ms). La ortografía de los subtítulos sale del guion (tildes, ¿¡, «…»), revisada en `subtitles.srt`: 28 bloques de 1–2 líneas, cortados por puntuación.
  - Nota: en el archivo largo whisper sin prompt transcribe «Tilo en voz alta» por «¡Dilo en voz alta!» (la /d/ de Kokoro es suave). La WAV de la escena 5 transcrita sola da «Dilo en voz alta», así que es un efecto del contexto de decodificación de whisper, no de la voz. «¡Sí!» aislado sonaba mal («Seír»): se corrigió uniéndolo a la frase siguiente en el texto del TTS, y ahora se oye «Sí».
- [x] **`mix.wav` a −14 LUFS ±1:** −14.34 LUFS integrados, true peak −1.49 dBTP. Música (medida tras el ducking, RMS relativo a la voz): −19.4 dB bajo la voz y −12.3 dB en los huecos. Efectos a −10 dB (LUFS relativo). *Corregido:* la primera mezcla dejaba la música en −24.9 / −17.8 dB; se subió 5.5 dB.
- [x] **Revisión visual** (`renderAt` en 0, 3, 6 … 63 s y en 63.26 s = último frame; 23 capturas mirando todas, en `snaps/qa_previo_1.png` y `snaps/qa_previo_2.png`, más capturas extra de las transiciones):
  - Estilo de papel rasgado, gouache, grano y crayón consistente en las 7 escenas; boil a 8 fps y movimiento a 12 fps.
  - Lía siempre igual (bob negro, gancho amarillo, suéter teal con cuello blanco, crayón azul): grande en las escenas 1 y 7 y pequeña a la derecha en las 2–6.
  - Nada importante en los 230 px superiores (solo estrellitas decorativas) ni en los 120 px de la derecha (solo el borde del marco). Los subtítulos van en la franja marina (y≈1466–1694), siempre en 1–2 líneas y sin cortes; ninguno se superpone al escenario.
  - Ningún frame vacío; la pausa del quiz deja la franja de subtítulos vacía a propósito (tic-tac del relojito).
  - Movimiento desde el frame 1: líneas de idea que pulsan, deriva de cámara y boil; el título empieza a dibujarse a los 0.25 s.
  - **Loop:** el primer (0 s) y el último frame (63.26 s) son el mismo escenario amarillo con los dibujos del gatito y del perrito, Lía con el crayón levantado y líneas de idea. El hilito del recap se borra en el último segundo para que coincidan.
  - *Corregido antes del render:* la tele de la escena 3 tocaba el telón (se movió y redujo); el sol del parque quedaba medio tapado detrás de la niña y del telón (se quitó); el halo de Lía dejaba una mancha verdosa en la escena 6 (se desactivó); la entrada por la boca del gato mostraba rosa desde el primer frame y con poco zoom (ahora se ve la boca oscura que se abre y deja ver el cerebro, con zoom ×9).
- [x] **Cada dato del guion está respaldado en `research.md`:** contagio al oír la palabra (#1), qué es un bostezo, sin la idea errónea del oxígeno (#2), el cerebro copia sin querer (#3), más contagio con familia y amigos, con «los científicos creen» (#4), los perros y su dueño (#5), los chimpancés (#6).
- [x] **El bucle abierto de la escena 2 se cierra en la escena 6:** «Y al final verás quién más se contagia» (con una cola misteriosa y un «?» detrás del cojín) → quiz de la escena 5 sobre el perrito → escena 6: «¡Sí! Muchos perros bostezan cuando ven bostezar a su dueño… Y los chimpancés también».
- [x] **Lía cierra con el CTA exacto:** «¡Sígueme para aprender más!» (whisper sin prompt: «Sígueme para aprender más.»), saludando con la mano.
- [x] **Todo el texto y el audio en `es`:** voz en español neutro (tú, sin modismos de un país), título en crayón «¿Por qué se contagian los bostezos?», opciones «SÍ» / «NO», subtítulos y metadatos en español.

**Resultado: todos los puntos se cumplen; se puede renderizar.**

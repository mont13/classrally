# Audio soubory pro ClassRally

MP3 soubory nejsou soucasti repozitare (licence). Nahraj si vlastni do teto slozky.

## Jak to funguje

Server automaticky nacte vsechny audio soubory z teto slozky (MP3, OGG, WAV, M4A, AAC)
a nahodne je vybira behem hry:

- **Loopy** (hudba na pozadi) - soubory, ktere NEobsahuji v nazvu klicova slova nize
- **Stingery** (kratke efekty) - soubory obsahujici v nazvu: `stinger`, `reveal`, `hit`, `win`, `ding`, `correct`, `lock`, `end`

Pokud zadne MP3 nejsou, host obrazovka pouzije **synth fallback** (WebAudio).

## Priklad

```
static/audio/
  tension-loop.mp3          -> loop (hudba pri otazce)
  quiz-start-emotions.mp3   -> loop
  reveal-stinger.mp3         -> stinger (efekt pri odhaleni)
  last-second-heartbeat.mp3  -> loop (dramaticky)
```

Staci nahrat libovolne soubory - cim vice, tim vetsi variabilita.

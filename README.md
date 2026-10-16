# SportCamEvaluator

Ocena jakości kompresji wideo (H.264 / H.265 / AV1 / VP9) w skali 0–100 **bez pliku źródłowego**.
100 = brak widocznych śladów kompresji, 0 = maksymalne zniekształcenia.

## Uruchomienie

1. **ffmpeg** – uruchom `get_ffmpeg.ps1` (prawy klik → *Uruchom za pomocą programu PowerShell*).
   Pobierze pełną wersję ffmpeg z libvmaf do podfolderu `ffmpeg\`.
   Alternatywnie: skopiuj `ffmpeg.exe` i `ffprobe.exe` do `ffmpeg\` albo wskaż folder w menu
   *Ustawienia → Wskaż folder z ffmpeg*. Bez libvmaf program działa, ale nie liczy CAMBI.
2. **Program**
   - z Pythonem 3.9+ (python.org): `run.bat`
   - jako EXE: `build_exe.bat` → `dist\SportCamEvaluator\SportCamEvaluator.exe`
3. *Dodaj pliki i analizuj…* – można wybrać wiele plików naraz, są analizowane po kolei.

Tryb wsadowy (konsola): `py sportcam_evaluator.py --cli plik1.mp4 plik2.mov --json wynik.json`

## Co jest mierzone

| Dane | Skąd | Do czego |
|---|---|---|
| **QP** każdej ramki | nagłówki strumienia (`-bsf:v trace_headers`), bez dekodowania. H.264/H.265: slice QP, AV1/VP9: `base_q_idx` | główny wskaźnik strat (waga 70%) |
| **Rozmiar i typ ramek** (I/P/B) | te same pakiety | bity na piksel, GOP |
| **SI / TI** | filtr `siti` (ITU-T P.910) | złożoność treści – normalizacja bitrate (waga 30%) |
| **CAMBI** | `libvmaf` (Netflix) | banding w gradientach – kara |

Ocena liczona jest w oknach 1 s. Wynik = 75% średniej + 25% najgorszych 5% okien.

Skala: 90+ doskonała · 75+ bardzo dobra · 60+ dobra · 40+ przeciętna · 20+ słaba · <20 bardzo słaba

## Kalibracja

Krzywe QP→ocena, przeliczenie AV1 `base_q_idx`→QP H.265 i przesunięcie dla H.264 dobrano tak,
żeby wyniki dla H.264, H.265 i AV1 przy różnych CRF układały się w tej samej kolejności i na podobnym
poziomie co VMAF (liczony ze źródłem). Przykład z testu (ten sam materiał 720p):

| Plik | VMAF | Wynik |
|---|---|---|
| x265 CRF 22 | 80 | 93 |
| x265 CRF 28 | 75 | 76 |
| AV1 CRF 44 | 75 | 76 |
| x264 CRF 26 | 76 | 77 |
| x265 CRF 34 | 66 | 53 |
| x265 CRF 40 | 50 | 31 |

To heurystyka, nie norma. Parametry są w `scoring.json` (menu *Ustawienia → Otwórz plik
parametrów oceny*; plik tworzy się przy pierwszym otwarciu). Najlepsza dalsza kalibracja:
nagraj kilka scen w wysokiej jakości, zakoduj z różnymi CRF, policz VMAF i dopasuj krzywe.

## Ograniczenia

- Mierzy ślady kompresji, nie ostrość optyki, ekspozycję ani szum sensora.
- Mocno zaszumiony obraz podnosi QP i bitrate – porównuj kamery na tej samej scenie.
- Odczytywany jest QP plastra/ramki; adaptacyjna kwantyzacja per blok nie jest widoczna.
- Bardzo długie pliki 4K: dekodowanie trwa; CAMBI liczone jest domyślnie na ok. 400 klatkach
  (opcja *co ile klatek*).

## Pliki

- `sportcam_evaluator.py` i `evaluator_core.py` – zgodne punkty wejścia GUI, API i CLI.
- `core/` – analizator, dekoder nagłówków, wykonanie filtrów pikselowych, segmenty, sprzęt, procesy, scoring, eksport i CLI.
- `gui/` – okno główne, wykresy, kontrolki i motyw.
- `get_ffmpeg.ps1`, `run.bat`, `build_exe.bat` – pobieranie FFmpeg, uruchamianie i budowanie EXE.
- Ustawienia użytkownika: `%APPDATA%\SportCamEvaluator\config.json`


## Wydajność (1.3.0)

- **Zrównoważony**: pełne QP i SI/TI, dotychczasowy automatyczny odstęp CAMBI.
- **Dokładny**: pełne QP i SI/TI, CAMBI co klatkę (można wskazać inny odstęp).
- **Szybki**: pełne QP, ciągłe próbki SI/TI i CAMBI z początku, środka, końca oraz fragmentów o wysokim QP / rozmiarze klatek. Wynik jest oznaczony jako przybliżony.
- CAMBI-only jest włączane dopiero po sprawdzeniu identyczności wyników 8/10-bit oraz braku metryk modelu VMAF. Nie zmienia rozdzielczości CAMBI. W razie nieobsługi wraca model VMAF.
- Dekoder i podział pracy są dobierane medianami z trzech powtórzeń pomiaru całego łańcucha filtrów. Kalibracja sprawdza CPU, CUDA/NVDEC, D3D11VA, 1/2/3/4/6 procesów i wątki filtrów, z osobnym dłuższym pomiarem pamięci. Jej czas zależy od materiału i sprzętu; na pierwszym długim 4K 10-bitowym materiale zajęła około 11 min. Profil jest zapisywany według sprzętu, sterownika, FFmpeg, kodeka, rozdzielczości, głębi i ustawień, a zgodne kolejne analizy omijają autotuning. Tryb Szybki porównuje tylko dekodery. Pełny benchmark można uruchomić ręcznie. Koszt pierwszej kalibracji może przewyższyć zysk dla pojedynczego filmu.
- Ręczny benchmark (bez rekompresji i bez plików wideo):
  `python evaluator_core.py "film.mp4" --benchmark`
  Wynik pokazuje CPU/CUDA/D3D11VA i działające backendy oraz testy 1/2/3/4/6 procesów. Warianty z różnymi metrykami są odrzucane. Kalibracja jest zapamiętywana.
- Cache zakończonych analiz: `%LOCALAPPDATA%\SportCamEvaluator\cache-v2`. GUI: **Wymuś analizę**, menu **Ustawienia → Wyczyść cache wyników**. W CLI użyj `--force`. Cache uwzględnia metadane pliku z czasem nanosekundowym, wersję silnika, scoring, tryb i ustawienia metryk oraz pliki FFmpeg/ffprobe.
- CLI: `python evaluator_core.py film.mp4 --mode fast|balanced|accurate --force --json wynik.json`.
- Eksport JSON zawiera czasy etapów, dekoder, liczbę/wątki procesów, FPS i próbkowany szczyt sumy working set Python + FFmpeg. To pomiar co 100 ms, obejmuje strony współdzielone wielokrotnie i nie jest dokładnym maksimum fizycznego RAM.
- Wykresy są agregowane poza głównym wątkiem do kolumn pikseli z zachowaniem minimum/maksimum. Ukryte zakładki nie są rysowane. Surowe dane pozostają w eksporcie i podpowiedziach.
- Pasek postępu pokazuje klatki ukończone w obu pełnych przebiegach, FPS chwilowe i średnie, pozostałe klatki, ETA po rozgrzaniu estymatora, dekoder i aktywne procesy.
- Punktacja i jej wagi pozostają bez zmian. Poprawiono ciągłość TI, nadmiarowe klatki na granicach segmentów oraz globalną fazę próbkowania CAMBI. Te poprawki mogą zmienić stare oceny; szczegóły i pomiary są w `Raporty/`.

Testy: `python -m unittest discover -s tests -v`.

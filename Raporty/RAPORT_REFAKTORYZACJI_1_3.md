# Raport SportCamEvaluator 1.3

Data pomiarów: 16.10.2026. Testy wykonano na komputerze z NVIDIA RTX 5070 Ti, 32 wątkami CPU i FFmpeg `N-127564-g3c92f003c7-20261015`.

## Wynik na długim filmie

Materiał `DJI_20261006062240_0005_D.MP4`: HEVC Main 10, 3840×2160, 29,97 FPS, BT.709, 56 453 klatki, 31:23.648. Analiza w trybie Zrównoważonym obejmuje wszystkie klatki SI/TI i pełną rozdzielczość; CAMBI używa ustawionego próbkowania `n_subsample=141`.

| Przebieg | Czas | Przepustowość względem klatek źródłowych | Szczytowa pamięć | Dekoder |
|---|---:|---:|---:|---|
| Pierwsze pełne uruchomienie 1.3, wraz z kalibracją | 26:58 (1617,77 s) | 34,90 FPS | 5,08 GB | CUDA, 6 procesów |
| Ponowne pełne uruchomienie z profilem `filter_complex_threads=2` | 16:00 (960,26 s) | 58,79 FPS | 5,05 GB | CUDA, 6 procesów |
| Pełny przebieg z końcowym profilem `filter_complex_threads=4` | 14:56 (895,90 s) | 63,01 FPS | 5,13 GB | CUDA, 6 procesów |

Pierwsze uruchomienie zużyło 679,79 s na kalibrację; pozostałe 937,98 s to analiza. Powtórzenie ze starym profilem `filter_complex_threads=2` zajęło 960,26 s, a z końcowym profilem `=4` 895,90 s: o 6,7% krócej względem poprzedniego pełnego przebiegu i o 4,5% względem analizy w pierwszym przebiegu po odjęciu kalibracji. W obu porównywanych pełnych przebiegach zebrano identyczne 56 453 klatki i 1884 jednosekundowe okna. Ocena wyniosła 93,6 („Doskonała”); średnie QP 26,5, SI 58,7, TI 16,5, CAMBI 0,04 (p95 0,28, max 1,0). Porównanie tablic wszystkich okien wykazało dokładną zgodność. Końcowy przebieg zakończył się bez ostrzeżeń i bez danych diagnostycznych na stderr.

Poprzednia próba na wersji 1.2 trwała około 42 minut, ale nie zachował się kompletny wynik JSON, więc jest to wyłącznie orientacyjna obserwacja, nie porównywalny pomiar.

## Diagnoza i dobór konfiguracji

Osobne testy początku, środka i końca długiego filmu wykonano po trzy razy. W wybranej ścieżce CUDA mediana czasu na trzy sekundy źródła wyniosła: samo dekodowanie 0,745–0,861 s (104,5–120,7 FPS), konwersja formatu 0,823–0,956 s (94,1–109,3 FPS), SI/TI 3,867–3,883 s (około 23,2 FPS), CAMBI 1,799–1,909 s (47,1–50,0 FPS), a wspólny potok SI/TI + CAMBI 5,286–5,347 s (16,8–17,0 FPS). Największym kosztem jest filtr SI/TI; wspólne dekodowanie dla SI/TI i CAMBI jest szybsze niż uruchomienie tych dwóch analiz osobno.

Trzy pomiary na każdym poziomie logowania (`debug`, `verbose`, `warning`, `error`) dały różnice median mniejsze niż 0,4%, bez mierzalnego wpływu obróbki stderr na prędkość. `error` zmniejsza jednak ilość zbędnego wyjścia (w kontrolowanym teście 0 B zamiast około 99 KB przy `debug`). Zweryfikowany profil używa teraz `-loglevel error`.

Końcowy autotuner zmierzył każdy backend trzykrotnie na tym samym 6-sekundowym wycinku. Mediany potoku wyniosły: CPU 11,095 s (16,21 FPS), CUDA 10,917 s (16,47 FPS), D3D11VA 11,065 s (16,25 FPS). Wszystkie konfiguracje dały dokładnie równe metryki, a przewaga CUDA nad CPU wyniosła 1,6%, więc wybór GPU opiera się na pomiarze kompletnego potoku, a nie na procentowym wykorzystaniu GPU. Pełna analiza na profilu CUDA z `filter_complex_threads=4` również wykazała identyczne wyniki względem profilu `=2`.

| Procesy FFmpeg | Mediana 6 s próbki | Przepustowość | Szczytowy working set w osobnym 24 s teście |
|---:|---:|---:|---:|
| 1 | 10,940 s | 16,44 FPS | 2,03 GB |
| 2 | 6,850 s | 26,25 FPS | 2,49 GB |
| 3 | 5,048 s | 35,62 FPS | 2,80 GB |
| 4 | 4,092 s | 43,94 FPS | 3,41 GB |
| 6 | 3,342 s | 53,81 FPS | 4,50 GB |

Przy sześciu procesach test wątków filtrów wykazał medianę 3,183 s dla `filter_complex_threads=2` i 3,109 s dla `=4` (przyspieszenie 2,3% na próbce); oba warianty zachowały dokładną zgodność. Dlatego nowy profil rekomenduje CUDA, 6 procesów, 2 wątki na proces i 4 wątki filtra. Wcześniejszy pełny test `filter_complex_threads=2` zachowuje dokładność, ale raportowany poniżej pełny przebieg `=4` zweryfikuje najnowszy profil end-to-end.

Pomiar pamięci sumuje working set procesu Python i aktywnych procesów FFmpeg co 100 ms; współdzielone strony mogą być policzone wielokrotnie. Autotuner rezerwuje co najmniej 55% dostępnej pamięci dla systemu i aplikacji, mierzy pamięć na dłuższym przebiegu na konfigurację, a szybkość porównuje na trzech powtórzeniach krótkiego zakresu. W próbkach 24-sekundowych poprzednie pomiary pokazały wzrost pamięci wraz z równoległością; w pełnym przebiegu sześciu workerów szczyt wyniósł około 5,05 GB, przy ponad 20 GB wolnej pamięci systemowej. Nie zaobserwowano oznak stronicowania ani błędu OOM.

## Refaktoryzacja i zachowana funkcjonalność

Logika została rozdzielona według odpowiedzialności w pakietach `core/` i `gui/`. `evaluator_core.py` (1543 → 25 linii) i `sportcam_evaluator.py` (1100 → 9 linii) zachowują punkty wejścia i API zgodności. Dodano 11 modułów rdzenia i 5 modułów GUI. Klasa `Analyzer` zmalała z 1198 do 284 linii; jej najdłuższa metoda `_analyze` ma 126 linii, wobec 236 linii najdłuższej metody przed podziałem. Klasa GUI pozostała podobnej wielkości, ale wykresy, widżety i okno główne mają osobne moduły.

Zachowane są tryby Szybki/Zrównoważony/Dokładny, metryki i matematyka oceny, jednosekundowe okna, cache, JSON/CSV, wykresy, CAMBI-only, anulowanie oraz CUDA z fallbackiem CPU. Poprawiono wyznaczanie liczby klatek segmentu na podstawie PTS, dzięki czemu granice nie opierają się na zaokrąglonym nominalnym FPS. Postęp równoległych zadań pokazuje liczbę klatek, procent, bieżący i uśredniony FPS, stabilizowane ETA, dekoder oraz liczbę aktywnych procesów.

## Testy i ograniczenia

- 16 testów jednostkowych przechodzi; opcjonalny test integracyjny GPU/fallback wymaga zmiennej `SPORTCAM_TEST_VIDEO` i jest raportowany oddzielnie.
- Pełna analiza HEVC 4K 10-bit 29,97 FPS obejmuje 56 453 klatki i trwała 16:00 z zapisanym profilem. Pełna analiza HEVC 4K 8-bit 29,98 FPS (`20260720-1500LOW.mp4`, 4 891 klatek) trwała 244,34 s łącznie z autotuningiem; średnia wyniosła 20,02 FPS, szczytowy working set 2,61 GB, ocena 3,8 i była zgodna z zachowanym wynikiem odniesienia 1.2.
- Krótki, 10-sekundowy HEVC 4K 10-bitowy wycinek przeanalizował się w 20,44 s trybem Zrównoważonym na CPU (14,7 FPS), bez cache; wynik 95,7.
- Kompilacja aplikacji PyInstaller one-file z kolekcją modułów `core` i `gui` zakończyła się poprawnie; plik EXE ma 10,5 MB.
- Dokładność metryk sprawdzono dla równoległości 1/2/3/4/6 oraz wariantów liczby wątków filtrów na identycznych zakresach PTS.
- Test anulowania i agregacji postępu równoległych segmentów jest objęty testami jednostkowymi.
- **NOT TESTED:** materiał 60 FPS; rzeczywisty materiał HDR (test 10-bit był BT.709, nie HDR).
- Pełny, porównywalny JSON z wersji 1.2 dla długiego filmu nie jest dostępny. Wykonanie CAMBI nadal kosztuje czas; największym pojedynczym kosztem pozostaje SI/TI.

## Pozostałe weryfikacje

Końcowy pełny przebieg z nowym profilem 6×2 i `filter_complex_threads=4` zakończył się w 895,90 s (14:56), uzyskując 63,01 FPS. Pamięć szczytowa 5 125 570 560 B; maksymalnie 6 procesów FFmpeg/ffprobe. Wszystkie jednosekundowe okna są równe przebiegowi z `filter_complex_threads=2`.

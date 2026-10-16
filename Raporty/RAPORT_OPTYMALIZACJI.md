# SportCamEvaluator — raport optymalizacji

Data: 16.10.2026. Silnik 1.2.0, rewizja `6-verified-native-download`.

## Wynik i zakres

Wprowadzono zmiany w istniejącej aplikacji: CAMBI-only z weryfikacją i fallbackiem, dobór dekodera i równoległości, ciągłość TI na granicach, tryby analizy, cache i rendering wykresów. Nie zmieniono wag, krzywych ani funkcji punktowania. Nie rekompresowano i nie utworzono żadnego filmu tymczasowego. Wszystkie przebiegi FFmpeg kończyły się wyjściem null; pliki tymczasowe zawierały wyłącznie metadane.

Ścisła identyczność kompletnych wyników z 1.1.0: **NIE**. Poprawiono nadmiarowe klatki, ciągłość TI, znaczniki czasu i fazę próbkowania CAMBI. Zmiany i ich wpływ są opisane poniżej; nie zastosowano tolerancji ukrywającej rozbieżności.

## Środowisko i punkt odniesienia

Windows, AMD Ryzen 9 5950X / 32 wątki, NVIDIA RTX 5070 Ti 16 GB, sterownik 610.88, Python 3.10.11. FFmpeg `N-127564-g3c92f003c7-20261015`. Oba wskazane filmy: HEVC 3840×2160, 8-bit, około 29,97 FPS i 163 s, 4891 pakietów wideo. Dodatkowa kontrola HDR: HEVC 4K 10-bit `GX020079.MP4` (krótkie fragmenty).

Projekt przed optymalizacją nie miał repozytorium Git. Kopie oryginalnych plików i pełne wyniki starej wersji zachowano lokalnie na potrzeby porównań; nie są częścią publicznego repozytorium. Stara aplikacja żądała D3D11VA i 6 procesów; nie raportowała rzeczywistego fallbacku. Jej faktycznego dekodera w historycznym pomiarze nie można potwierdzić. Nowy dekoder potwierdzany jest formatem sprzętowym wybranym przez FFmpeg, a nie samą dostępnością CUDA.

## Czasy — pełna analiza z zapisaną kalibracją

Całość oznacza czas ścienny obliczeń, z odczytem informacji, nagłówków i punktowaniem, przed końcowym zapisem cache i rysowaniem GUI. Nie sumowano czasów nakładających się etapów. SI/TI i CAMBI w tabelach to osobne testy na pierwszych 3 s, CPU, 8 wątków i ten sam n_subsample=12; ich czasów nie należy dodawać do czasu całego filmu. Są to pojedyncze pomiary, bez deklaracji istotności statystycznej.

### 20260720-1500.MP4

| Element | Stara wersja | Nowa wersja | Przyspieszenie |
|---|---:|---:|---:|
| QP — cały film | 3.940 s | 1.339 s | 2.94× |
| SI/TI — fragment 3 s | 4.233 s | 3.975 s | 1.06× |
| CAMBI — fragment 3 s | 2.071 s | 1.644 s | 1.26× |
| Cała analiza | 73.419 s | 64.618 s | 1.14× |
| FPS analizy | 66.62 | 75.69 | 1.14× |
| Wynik jakości | 91.9 | 91.8 | nie dotyczy |

Dekoder faktyczny: **cuda / NVDEC**, 4 procesy pikselowe; każdy: decoder_threads=3, filter_complex_threads=1, libvmaf_threads=3. Próbkowany szczyt sumy working set: 2.94 GiB. Maksimum zarejestrowanych procesów FFmpeg/ffprobe: 4.

### 20260720-1500LOW.mp4

| Element | Stara wersja | Nowa wersja | Przyspieszenie |
|---|---:|---:|---:|
| QP — cały film | 3.958 s | 1.497 s | 2.64× |
| SI/TI — fragment 3 s | 3.547 s | 3.497 s | 1.01× |
| CAMBI — fragment 3 s | 1.300 s | 0.848 s | 1.53× |
| Cała analiza | 73.589 s | 65.319 s | 1.13× |
| FPS analizy | 66.46 | 74.88 | 1.13× |
| Wynik jakości | 3.8 | 3.8 | nie dotyczy |

Dekoder faktyczny: **cuda / NVDEC**, 6 procesy pikselowe; każdy: decoder_threads=2, filter_complex_threads=1, libvmaf_threads=2. Próbkowany szczyt sumy working set: 3.36 GiB. Maksimum zarejestrowanych procesów FFmpeg/ffprobe: 6.

## Koszt pierwszego uruchomienia i cache

Pierwsza pełna analiza pierwszego filmu z kalibracją: **101.772 s**, w tym benchmark CPU/GPU/segmentów **34.779 s**. Ten pierwszy przebieg jest wolniejszy od 1.1.0; zysk pojawia się po zapamiętaniu konfiguracji. Cache gotowego wyniku na badanych plikach ładował się w około 0,016 s, bez procesu FFmpeg. Detekcja narzędzi przy tworzeniu Tools jest osobnym kosztem startowym aplikacji.

Cache: `%LOCALAPPDATA%\SportCamEvaluator\cache-v2`. Klucz zawiera ścieżkę, rozmiar, mtime_ns, rewizję silnika, scoring, tryb i ustawienia metryk oraz metadane plików FFmpeg/ffprobe. Zapis atomowy przez plik tymczasowy, fsync i replace. Zmiana pliku w trakcie analizy blokuje zapis. GUI ma wymuszanie analizy i czyszczenie cache wyników; kalibracja sprzętu pozostaje osobną kategorią.

## CPU a GPU

Porównanie na pierwszych 6 s głównego filmu, ta sama liczba procesów i ten sam budżet wątków:

| Backend | Procesy | Wątki dekodera / libvmaf | Czas | Zgodność metryk |
|---|---:|---:|---:|---|
| CPU | 3 | 4 / 4 | 7.018 s | True |
| CPU | 4 | 3 / 3 | 6.867 s | True |
| cuda | 3 | 4 / 4 | 4.055 s | True |
| cuda | 4 | 3 / 3 | 3.659 s | True |

W badanym fragmencie przy 4 procesach CUDA daje 1.88× większą przepustowość całego łańcucha niż CPU. To pomiar z transferem do RAM i filtrami, nie sam dekoder. Nie oznacza takiej samej przewagi w każdym pliku.

Pierwsza analiza LOW z własną kalibracją: 79.056 s; benchmark 32.889 s. Różne metadane źródeł poprawnie prowadzą do osobnych decyzji kalibracyjnych.

## Co przyspieszyło i jak

- CAMBI-only: `libvmaf=model=|:feature=name=cambi`, bez modeli i ich predykcji. Runtime sprawdza równość ekstraktora w 8/10-bit oraz to, że JSON zawiera wyłącznie CAMBI. Nie ma zmiany rozdzielczości, głębi ani matematyki CAMBI. Niezgodność lub błąd uruchomienia pozostawia/przywraca wariant z modelem. Filtr FFmpeg nadal wymaga dwóch wejść; ich usunięcie wymagałoby innego interfejsu natywnego.
- Kalibracja bada rzeczywistą ścieżkę dekodowanie + filtry. Liczba procesów 2/3/4/6, wątki dekodera, libvmaf i filter_complex mają wspólny budżet CPU oraz limit szacowany z wolnego RAM. Warianty zmieniające metryki są odrzucane. Cache kalibracji uwzględnia sprzęt, sterownik, kodek, format, rozdzielczość, głębię, kolory, obrót, dysk źródłowy, FFmpeg i ustawienia. Krótkie filmy bez kalibracji korzystają z CPU. Automatyczny benchmark jest ograniczony czasowo; ręczny może sprawdzić więcej wariantów.
- Zostawiono natywny transfer klatek FFmpeg do pamięci CPU: jedno dekodowanie, wspólne klatki dla SI/TI i CAMBI. Wariant z jawnymi powierzchniami sprzętowymi i hwdownload odrzucono po wykryciu regresji czasu oraz różnic HDR/rotacji. Dekoder jest potwierdzany komunikatem get_format; pełny strumień debug jest odrzucany w pamięci, na dysku pozostaje tylko ograniczony wyciąg diagnostyczny.
- Granice segmentów mają poprzednią klatkę dla TI i dokładny limit liczby klatek. PTS z nagłówków tworzą globalną oś czasu. CAMBI zachowuje wspólną fazę także przy bardzo rzadkim próbkowaniu. Jeśli nie ma wiarygodnego mapowania pakietów na klatki, pozostaje przebieg ciągły.
- Nagłówki kończą się przed segmentacją, ponieważ ich PTS określają granice. Parser trace_headers i jego wyniki pozostają niezmienione. Mniejsza konkurencja CPU skróciła zmierzony czas QP; nie jest to dowód przyspieszenia samego parsera. Natywnego parsera nie dodano: QP nie był wąskim gardłem całej analizy, a ryzyko regresji PPS/slice/AV1/VP9 nie było uzasadnione.
- Wykresy: agregacja w tle do kolumn pikseli, lokalne minima i maksima, surowe dane dla eksportu i podpowiedzi, rysowanie tylko widocznego wykresu, debounce zmiany rozmiaru. Przygotowanie serii i agregacja nie blokują Tkinter.

## Poprawność ocen

**20260720-1500.MP4**: wszystkie pakiety QP/typy/rozmiary/czasy są identyczne. SI/TI: stara 4895, nowa 4891 rekordów; CAMBI: 408 → 408. Zmienione wartości oceny dotyczą 80 okien jednosekundowych. Wynik 91.9 → 91.8, p5 86.2 → 85.5.

**20260720-1500LOW.mp4**: wszystkie pakiety QP/typy/rozmiary/czasy są identyczne. SI/TI: stara 4895, nowa 4890 rekordów; CAMBI: 408 → 408. Zmienione wartości oceny dotyczą 104 okien jednosekundowych. Wynik 3.8 → 3.8, p5 0.0 → 0.0.

Stara segmentacja dublowała obrazy i zastępowała TI na granicy wartością następnej klatki. Faza CAMBI zależała od miejsc podziału. Poprawki zmieniają dane wejściowe do tej samej funkcji score; dlatego nie deklaruję pełnej zgodności z 1.1.0. Test na identycznych danych potwierdza identyczność funkcji punktowania, wag i ocen wszystkich okien. Nowe pełne wyniki są dokładnie zgodne między różnymi poprawnymi podziałami; testy nie stosują tolerancji numerycznej.

LOW: sam FFmpeg bez filtrów dekoduje 4890 obrazów, mimo 4891 pakietów/nagłówkowego nb_frames. Aplikacja zachowuje surowe dane pakietów, nie uzupełnia obrazu sztucznie i pokazuje ostrzeżenie.

## Tryb szybki — błąd przybliżenia

| Film | Czas | Wynik szybki | Wynik pełny | Różnica |
|---|---:|---:|---:|---:|
| 20260720-1500.MP4 | 28.75 s | 93.6 | 91.8 | +1.8 pkt |
| 20260720-1500LOW.mp4 | 27.25 s | 5.6 | 3.8 | +1.8 pkt |

Próbki to ciągłe fragmenty początku, środka i końca oraz fragmenty wskazane przez QP i rozmiary pakietów. Mają klatkę poprzedzającą dla TI. Brakujące okna korzystają ze średnich próbek, co może powodować błąd. Wynik jest oznaczony jako PRZYBLIŻONY w GUI, JSON i CSV. Tryb Dokładny ustawia CAMBI co klatkę, chyba że użytkownik poda inny odstęp. Pełnej analizy obu filmów z CAMBI co klatkę nie wykonano; sprawdzono gęste CAMBI na rzeczywistych fragmentach 8/10-bit.

## GUI i niezawodność

Test 100 000 punktów QP: stary Canvas 100040 obiektów, nowy 895. Czas starego set/rysowania 0.391 s, nowy czas do gotowego wykresu z debounce i pracą w tle 0.172 s; samo rysowanie nowego Canvas 0.005 s. Zachowano skoki 0 i 51 oraz 100 000 surowych punktów.

Wykonano test kolejki GUI na obu prawdziwych wynikach z cache, przygotowania wszystkich pięciu wykresów, eksportu JSON/CSV, anulowania procesu potomnego, uszkodzonego źródła, atomowego zapisu i unieważniania cache. Wymuszony błędny backend GPU przechodzi na CPU także dla czterech segmentów. Nie uruchamia się ponownie całej analizy. Zamykanie GUI czeka na sprzątanie workerów. Postęp łączy nagłówki i piksele, a kalibracja nie fałszuje postępu filmu.

Testy automatyczne: **10/10 PASS**, bez ostrzeżeń o niezamkniętych potokach. Usunięto referencje do widgetów Tk z workerów obliczeniowych; zamknięcie czeka również na agregację wykresów.

## Nieprzetestowane i ograniczenia

- Rzeczywiste karty AMD i Intel: NOT TESTED. CPU fallback sprawdzony; D3D11VA sprawdzono na karcie NVIDIA.
- H.264, AV1, VP9, materiały 60 FPS, rzeczywiste VFR, pełna analiza HDR oraz niepełne/zmienne struktury pakietów: brak odpowiednich pełnych testów w tym zadaniu. Parser zachowano, nie dodawano zgadywania QP ani CRF.
- Nie wykonano automatycznego testu obrazu całego GUI na ekranie ani budowy EXE. Wykonano testy widgetów Tkinter, redukcji wykresu i rzeczywistej kolejki.
- Benchmark krótkiego fragmentu nie gwarantuje optimum na całym filmie. Koszt pierwszej kalibracji może przewyższyć zysk; zapisane decyzje i gotowe wyniki są ponownie używane. Zmienna przepustowość dysku i obciążenie systemu wpływają na pojedyncze pomiary.
- RAM to maksimum próbek co 100 ms, suma working set Python + zarejestrowane procesy FFmpeg/ffprobe; strony współdzielone mogą być policzone wielokrotnie. Nie jest to dokładny szczyt prywatnej fizycznej pamięci.

## Pliki i odtworzenie

Zmienione: `evaluator_core.py`, `sportcam_evaluator.py`, `README.md`. Dodane: `performance.py`, `benchmark_optimization.py`, `tests/test_optimization.py`. Nie było scoring.json; wartości domyślne punktacji pozostają takie jak w oryginale.

```powershell
python -m unittest discover -s tests -v
$env:SPORTCAM_TEST_VIDEO="ścieżka\do\filmu.mp4"
python -m unittest discover -s tests -v
python evaluator_core.py "ścieżka\do\filmu.mp4" --benchmark --json Raporty\kalibracja.json
python benchmark_optimization.py "ścieżka\do\filmu1.mp4" "ścieżka\do\filmu2.mp4" --output Raporty\powtorzenie.json
```

Podstawą decyzji CAMBI-only jest też kod pierwotny [filtra FFmpeg libvmaf](https://raw.githubusercontent.com/FFmpeg/FFmpeg/master/libavfilter/vf_libvmaf.c); obsługa model=| jest sprawdzana testem runtime, a nie zakładana dla innych buildów.

Wynik: **PASS dla przeprowadzonych testów nowej implementacji na opisanym sprzęcie i źródłach. Ścisła zgodność pełnego wyniku z 1.1.0 — NIE (opisane poprawki danych). Pozostałe backendy/kodeki — NOT TESTED.**

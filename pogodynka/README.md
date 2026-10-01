# Paczkomat Pogodynka

Twoja prywatna stacja pogodowa z czujników w paczkomatach InPost: temperatura, wilgotność, ciśnienie, PM1, PM2,5 i PM10.

- strona na telefon: wyszukiwanie po kodzie (np. `WRO358M`), lista najbliższych paczkomatów z czujnikami, ulubione,
- JSON do automatyki domowej (Home Assistant, Node-RED, cokolwiek),
- tylko Python 3.8+, bez dodatkowych bibliotek.

## Uruchomienie

```bash
cd pogodynka
python3 app.py
# -> http://localhost:8080
```

Zmienne środowiskowe: `PORT` (domyślnie 8080), `HOST` (0.0.0.0), `READING_TTL` (cache odczytów w sekundach, domyślnie 1800), `CACHE_DIR`.

Serwer musi działać na komputerze z dostępem do internetu (np. Raspberry Pi, NAS, serwer z Home Assistant).
Przeglądarka nie może pytać inpost.pl bezpośrednio (CORS), dlatego potrzebny jest ten pośrednik.

> Przycisk „📍 Najbliższe z czujnikiem” wymaga HTTPS albo `localhost`, bo tak działa geolokalizacja w przeglądarkach.
> W sieci domowej pod adresem `http://192.168.x.x:8080` szukaj po prostu po kodzie paczkomatu.

## Jak to działa

1. [ShipX API](https://api-shipx-pl.easypack24.net/v1/points) (publiczne, bez klucza) zwraca dane paczkomatu i listę najbliższych.
   Czujnik ma ten, który ma ustawione pole `air_index_level`.
2. Sitemapa inpost.pl (`/sitemap/points/N.xml`) prowadzi do strony paczkomatu. Pobierana raz na tydzień (kilkanaście sekund przy pierwszym zapytaniu).
3. Strona paczkomatu zawiera `data-shipx-url="/shipx-point-data/<ID>/<KOD>/air_index_level"`, z którego bierzemy numeryczne ID.
   ID jest zapamiętywane w `~/.cache/paczkomat-pogodynka/ids.json`.
4. `POST https://inpost.pl/shipx-point-data/<ID>/<KOD>/air_index_level` z nagłówkiem `X-Requested-With: XMLHttpRequest` zwraca odczyty.

Jeśli automatyczne znalezienie ID się nie uda, można je podać ręcznie: w aplikacji (pole pod błędem) albo w URL-u `/api/reading/WRO358M?id=12345`.

## API

`GET /api/reading/<KOD>`:

```json
{
  "code": "WRO358M", "id": 12345,
  "air_index_level": "GOOD", "air_quality": "dobra",
  "temperature": 14.3, "humidity": 71.0, "pressure": 1008.6,
  "pm1": 7.2, "pm25": 11.4, "pm4": null, "pm10": 18.0,
  "sensors": { "PM25": { "value": 11.4, "percent": 45.6 }, "...": {} },
  "updated": "2026-10-01T20:09:01+0200"
}
```

`GET /api/nearby?code=<KOD>` albo `GET /api/nearby?lat=51.11&lon=16.88` zwraca najbliższe paczkomaty z czujnikami.

## Home Assistant

Do `configuration.yaml` (podmień adres serwera i kod paczkomatu):

```yaml
rest:
  - resource: http://192.168.1.10:8080/api/reading/WRO358M
    scan_interval: 3600
    sensor:
      - name: "Paczkomat temperatura"
        value_template: "{{ value_json.temperature }}"
        unit_of_measurement: "°C"
        device_class: temperature
        state_class: measurement
      - name: "Paczkomat wilgotność"
        value_template: "{{ value_json.humidity }}"
        unit_of_measurement: "%"
        device_class: humidity
        state_class: measurement
      - name: "Paczkomat ciśnienie"
        value_template: "{{ value_json.pressure }}"
        unit_of_measurement: "hPa"
        device_class: atmospheric_pressure
        state_class: measurement
      - name: "Paczkomat PM2.5"
        value_template: "{{ value_json.pm25 }}"
        unit_of_measurement: "µg/m³"
        device_class: pm25
        state_class: measurement
      - name: "Paczkomat PM10"
        value_template: "{{ value_json.pm10 }}"
        unit_of_measurement: "µg/m³"
        device_class: pm10
        state_class: measurement
      - name: "Paczkomat jakość powietrza"
        value_template: "{{ value_json.air_quality }}"
```

## Uwagi

- To **nieoficjalne** API. Używaj go rozsądnie: odczyt raz na godzinę w zupełności wystarczy, czujniki i tak nie raportują częściej.
- Nie każdy paczkomat ma czujniki (w Polsce ok. 4 tys. z ponad 30 tys.). Aplikacja pokazuje te najbliższe, które mają.
- Temperatura i wilgotność są mierzone w obudowie paczkomatu, więc mogą odbiegać od rzeczywistości (np. w pełnym słońcu).
- Ciśnienie: nowsze czujniki podają rzeczywiste, starsze (wartości ok. 1020+) przeliczone do poziomu morza.

Pomysł: wątek [@uwteam_org](https://www.threads.net/@uwteam_org). Sposób szukania ID na podstawie [szmidtpiotr/air-locker-map](https://github.com/szmidtpiotr/air-locker-map).

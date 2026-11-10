# HUDY Deal Monitor – GitHub Actions, zdarma, ovládateľný z Androidu

## Čo skutočne funguje
- Python monitor, JSON-LD Product parser, konzervatívne objavovanie produktov zo seed URL a sitemap.
- SQLite história cien a nové minimum, Telegram upozornenia (pri potvrdenej veľkosti), workflow každých ~6 hodín.
- Konkurenti: iba manuálne uvedené URL a ručne potvrdená zhoda variantu. **Nie je to automatický vyhľadávač konkurencie.**

## Čo ešte vyžaduje overenie na živom HUDY
- Aktuálna štruktúra HTML, či stránky poskytujú JSON-LD Product, veľkosti jednotlivých dostupných variantov a pôvodnú cenu.
- Parser **zámerne neurčuje pôvodnú cenu**, preto sa upozornenia na 50 % zľavy zatiaľ nespustia. História a nové minimá fungujú, ak stránka obsahuje cenu a overiteľnú veľkosť.
- Vypredané veľkosti a rozdielne varianty sa bez ďalšej úpravy parsera nedajú vždy spoľahlivo rozlíšiť. Nevkladajte falošné ceny alebo dostupnosť.

## Inštalácia len z Androidu
1. V mobilnom prehliadači otvorte github.com, prihláste sa a vytvorte **nový verejný repozitár** (napr. `hudy-deal-monitor`). Verejný repozitár znamená, že história cien v SQLite bude verejná. **Tokeny nikdy neukladajte do súborov repozitára.**
2. ZIP rozbaľte v Správcovi súborov. GitHub mobilný web nevie vždy jednoducho nahrať celý strom vrátane skrytého `.github`. Najspoľahlivejšie je v prehliadači zapnúť **Desktop site** a súbory nahrať cez `Add file > Upload files`; skontrolujte, že je prítomný `.github/workflows/monitor.yml`. Ak mobilné nahrávanie zachováva iba jednotlivé súbory, vytvorte workflow priamo cez `Add file > Create new file` s názvom `.github/workflows/monitor.yml` a vložte obsah zo ZIP.
3. V Telegrame vytvorte bota cez @BotFather, napíšte mu `/start`. Zistite `chat_id` cez `https://api.telegram.org/bot<TOKEN>/getUpdates` (URL si vytvorte u seba, token nezdieľajte).
4. GitHub > repozitár > Settings > Secrets and variables > Actions > New repository secret: pridajte `TELEGRAM_BOT_TOKEN` a `TELEGRAM_CHAT_ID`.
5. V `config.example.yaml` upravte `seed_urls` na reálne dostupné výpredajové kategórie HUDY, ak sa pôvodná URL zmenila.
6. V `Actions` zapnite workflow (ak GitHub vyžaduje) a spustite `HUDY Deal Monitor > Run workflow`.
7. Otvorte log kroku `Monitor HUDY`. Ak vypíše `WARNING: no matching structured product data found`, treba upraviť parser podľa skutočného HTML. **Žiadne úspešné upozornenie tým nie je garantované.**
8. Ak monitor načíta produkty, súbor `monitor.sqlite3` sa po úspešnom behu commitne do repozitára a pri ďalšom behu sa znova načíta. Ak repozitár zakazuje push z Actions, v Settings > Actions > General nastavte `Workflow permissions > Read and write permissions` (ak je dostupné).

## Bezpečnosť a limity
- GitHub Actions na verejnom repozitári so štandardnými GitHub-hosted runnermi je spravidla bezplatný; dodržiavajte aktuálne limity a podmienky GitHubu.
- Cron nie je presný, môže meškať; GitHub môže plánované workflow pri dlhšej neaktivite vypnúť.
- SQLite história v hlavnej vetve vytvára commit pri každom úspešnom behu. Nenechávajte v DB osobné údaje.
- Web sa prehľadáva s obmedzením počtu stránok, oneskorením a kontrolou robots.txt. Pri blokovaní monitor nezvyšujte agresivitu.
- `--dry-run` netelegramuje; napriek tomu zapisuje lokálnu SQLite databázu.

## Lokálne testovanie (voliteľné)
```
pip install -r requirements.txt
cp config.example.yaml config.yaml
python -m unittest discover -s tests -v
python -m hudy_monitor --dry-run
```

## Konfigurácia
Pozrite `config.example.yaml`: značky, `min_discount_percent: 50`, `clothing_sizes`, `shoe_sizes`, seed URL a voliteľné manuálne overené konkurenčné URL.

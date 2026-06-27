# CONTEXT — Drive Website Bot

> Handoff-bestand. Laad dit in aan het begin van een volgende chat om de volledige context te herstellen.
> Laatst bijgewerkt: 27 juni 2026.

## Wat is dit project
De website van coverband **Drive** wordt aangepast via een **Telegram-bot**. Bandleden sturen een
berichtje of een foto naar de bot (privé of in de bandgroep); Claude past de website-HTML aan en zet
het direct live. Setlist-wijzigingen lopen via aparte commando's.

## Repos (GitHub, owner: KobusvanderZwaal — beide publiek)
- **Website:** `Website_Drive` → live op GitHub Pages: https://kobusvanderzwaal.github.io/Website_Drive/
  - Hoofdbestand: `index.html` (foto's staan inline als base64 + in `images/`).
- **Bot:** `Website_Drive_bot` → `main.py` (Python, python-telegram-bot 20.x).
  - Links: https://github.com/KobusvanderZwaal/Website_Drive_bot en https://github.com/KobusvanderZwaal/Website_Drive

## Hosting (Railway)
- Project **Website-Drive-bot**, environment **production**, service **worker** (Python 3.12, long-polling, US West).
- Service-URL: https://railway.com/project/b650e6fd-7573-4ab5-a843-a6329a5f8090/service/0b41a6b7-e9c5-4048-b393-ab74416242d6
- Railway redeployt automatisch bij elke push naar `main`.
- **Variabelen in Railway** (de geheime staan ALLEEN in Railway, NIET in dit bestand):
  - Geheim: `TELEGRAM_TOKEN`, `GITHUB_TOKEN`, `ANTHROPIC_API_KEY`
  - Config: `ALLOWED_USERS` = `8990259893` (Kobus' Telegram-ID), `GROUP_CHAT_ID` = `-5349146756` (bandgroep)

## Hoe de bot werkt (huidige versie)
- **Directe verwerking, GEEN wachtrij, GEEN nachtelijke batch.** Een vrije-tekstbericht of een
  foto-met-bijschrift gaat naar Claude (`claude_edit`, tool-based JSON), die `index.html` aanpast en
  meteen commit. Site is binnen ~1 minuut bijgewerkt.
- **Setlist (direct live):** `/setlist`, `/toevoegen Artiest | Titel | Decennium`, `/verwijderen Titel`.
- **Werkt in privéchat én de bandgroep.** In de groep mag **iedereen** wijzigen (de `is_allowed()`-helper
  slaat de `ALLOWED_USERS`-check over als de chat de geconfigureerde groep is); in privé alleen `ALLOWED_USERS`.
- **Bot is admin in de bandgroep** — nodig zodat hij alle groepsberichten ziet (anders blokkeert Telegram's
  privacy-modus gewone berichten).
- **Foto's:** als échte foto (gecomprimeerd) **mét bijschrift** sturen. Bestanden/documenten worden genegeerd.
- Overige commando's: `/start`, `/help`, `/chatid`.

## Belangrijke valkuilen (lessen uit eerdere sessies)
- **GitHub-commits zijn ONZICHTBAAR in de Railway-logs** — de bot doet GitHub-werk via de `requests`-library,
  die niets logt. Alleen Telegram- en Claude-aanroepen (httpx) verschijnen als "HTTP Request:"-regels.
  Conclusie "geen commit gebeurd" NOOIT baseren op de Railway-logs; check de repo-commits zelf.
- Een korte **`409 Conflict`-traceback** vlak na elke deploy = deploy-overlap (oude + nieuwe instance lopen
  ~1 min samen). Onschuldig.
- **`raw.githubusercontent.com` kan minuten gecachet zijn.** Gebruik de GitHub blob-/edit-pagina voor de
  écht actuele bestandsinhoud.
- Berichten/foto's in de **groep** vereisen dat de bot **admin** is én dat `GROUP_CHAT_ID` is gezet.

## Status (27 juni 2026)
- Alles live en werkend. Laatste relevante bot-commit: **"Groep: iedereen in de bandgroep mag wijzigen (is_allowed)"**.
- Groep-ondersteuning actief: `GROUP_CHAT_ID` gezet in Railway + bot is admin in de groep.
- Eerder opgelost: bot draaide al (was geen crash); foto kwam binnen toen privé + als foto verstuurd;
  een testfoto staat live onder het kopje "20 juni 2026" op de site.

## Hoe testen
Stuur in de bandgroep een gewoon berichtje (bijv. "zet de intro-tekst op …") of een foto met bijschrift.
Binnen ~1 minuut is de site bijgewerkt. Controleren kan via de commits in `Website_Drive` of door de site
te verversen.

## Snelle links
- Site: https://kobusvanderzwaal.github.io/Website_Drive/
- Bot-repo: https://github.com/KobusvanderZwaal/Website_Drive_bot
- Website-repo: https://github.com/KobusvanderZwaal/Website_Drive
- Railway-service: https://railway.com/project/b650e6fd-7573-4ab5-a843-a6329a5f8090/service/0b41a6b7-e9c5-4048-b393-ab74416242d6

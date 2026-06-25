#!/usr/bin/env python3
"""
Drive Band Website Bot
Hiermee kunnen bandleden de setlist en de rest van de website aanpassen via Telegram.

Setlist-commando's (/toevoegen, /verwijderen) werken direct.
Vrije-tekst wijzigingen en foto's worden nu OOK direct verwerkt door Claude
en meteen op de website gezet — geen wachtrij meer.
"""

import os
import re
import json
import base64
import asyncio
import logging
import datetime
import requests
import anthropic
import pytz
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Config uit omgevingsvariabelen ─────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
GITHUB_TOKEN   = os.environ['GITHUB_TOKEN']
ALLOWED_USERS  = set(
    int(x.strip()) for x in os.environ.get('ALLOWED_USERS', '').split(',') if x.strip()
)
GROUP_CHAT_ID  = os.environ.get('GROUP_CHAT_ID', '').strip()
CLAUDE_MODEL   = os.environ.get('CLAUDE_MODEL', 'claude-opus-4-8')

REPO_OWNER = 'KobusvanderZwaal'
REPO_NAME  = 'Website_Drive'
BOT_REPO   = 'Website_Drive_bot'
FILE_PATH  = 'index.html'
BRANCH     = 'main'

API        = 'https://api.github.com'
GH_HEADERS = {
    'Authorization': f'token {GITHUB_TOKEN}',
    'Accept': 'application/vnd.github.v3+json'
}

TZ = pytz.timezone('Europe/Amsterdam')

VALID_DECADES = ['60s', '70s', '80s', '90s', '00s', '10s', '20s']


# ── GitHub hulpfuncties ────────────────────────────────────────────────────────

def get_html():
    url = f'https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{BRANCH}/{FILE_PATH}'
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def commit_files(files, commit_msg):
    """Commit één of meer bestanden (pad -> bytes) via de Git Data API."""
    ref_url  = f'{API}/repos/{REPO_OWNER}/{REPO_NAME}/git/refs/heads/{BRANCH}'
    ref_data = requests.get(ref_url, headers=GH_HEADERS, timeout=30)
    ref_data.raise_for_status()
    latest_sha = ref_data.json()['object']['sha']

    commit_data = requests.get(
        f'{API}/repos/{REPO_OWNER}/{REPO_NAME}/git/commits/{latest_sha}',
        headers=GH_HEADERS, timeout=30
    )
    commit_data.raise_for_status()
    base_tree = commit_data.json()['tree']['sha']

    tree_entries = []
    for path, content in files.items():
        blob = requests.post(
            f'{API}/repos/{REPO_OWNER}/{REPO_NAME}/git/blobs',
            headers=GH_HEADERS,
            json={'content': base64.b64encode(content).decode('utf-8'),
                  'encoding': 'base64'},
            timeout=60
        )
        blob.raise_for_status()
        tree_entries.append({'path': path, 'mode': '100644',
                             'type': 'blob', 'sha': blob.json()['sha']})

    tree = requests.post(
        f'{API}/repos/{REPO_OWNER}/{REPO_NAME}/git/trees',
        headers=GH_HEADERS,
        json={'base_tree': base_tree, 'tree': tree_entries},
        timeout=30
    )
    tree.raise_for_status()

    new_commit = requests.post(
        f'{API}/repos/{REPO_OWNER}/{REPO_NAME}/git/commits',
        headers=GH_HEADERS,
        json={'message': commit_msg, 'tree': tree.json()['sha'],
              'parents': [latest_sha]},
        timeout=30
    )
    new_commit.raise_for_status()

    result = requests.patch(
        ref_url,
        headers=GH_HEADERS,
        json={'sha': new_commit.json()['sha']},
        timeout=30
    )
    result.raise_for_status()


def commit_html(html, commit_msg):
    commit_files({FILE_PATH: html.encode('utf-8')}, commit_msg)


# ── Setlist-functies ───────────────────────────────────────────────────────────

def song_count(html):
    return len(re.findall(r"\{ decade:", html))


def update_count(html, count):
    return re.sub(r'\d+ nummers', f'{count} nummers', html)


def add_song(html, artist, title, decade):
    new_entry = f"            {{ decade: '{decade}', title: \"{title}\", artist: '{artist}' }},\n"
    positions = [m.start() for m in re.finditer(rf"decade: '{decade}'", html)]
    if positions:
        line_end = html.find('\n', positions[-1])
        html = html[:line_end + 1] + new_entry + html[line_end + 1:]
    else:
        close = html.rfind('];')
        line_start = html.rfind('\n', 0, close)
        html = html[:line_start + 1] + new_entry + html[line_start + 1:]
    return update_count(html, song_count(html))


def remove_song(html, title):
    pattern = rf'[ \t]*\{{[^}}]*title: ["\']?{re.escape(title)}["\']?[^}}]*\}}[,]?\n'
    new_html, n = re.subn(pattern, '', html, flags=re.IGNORECASE)
    if n == 0:
        return None, 0
    count = song_count(new_html)
    return update_count(new_html, count), count


def setlist_text(html):
    pattern = r"decade: '(\w+)',\s*title: [\"']([^\"']+)[\"'],\s*artist: [\"']([^\"']+)[\"']"
    matches = re.findall(pattern, html)
    by_decade = {}
    for decade, title, artist in matches:
        by_decade.setdefault(decade, []).append(f"{title} – {artist}")
    lines = [f"\U0001f3b8 Setlist Drive ({len(matches)} nummers)\n"]
    for d in VALID_DECADES:
        if d in by_decade:
            lines.append(f"\n{d}:")
            for song in by_decade[d]:
                lines.append(f"  • {song}")
    return '\n'.join(lines)


# ── AI-bewerking van de website ────────────────────────────────────────────────

IMG_RE = re.compile(r'base64,[A-Za-z0-9+/=\s]{100,}')

def strip_images(html):
    blobs = []
    def repl(m):
        blobs.append(m.group(0))
        return f'base64,__IMG_{len(blobs) - 1}__'
    return IMG_RE.sub(repl, html), blobs


def restore_images(html, blobs):
    for i, blob in enumerate(blobs):
        html = html.replace(f'base64,__IMG_{i}__', blob)
    return html


CLAUDE_SYSTEM = """Je bewerkt de HTML van de website van coverband Drive (één HTML-bestand,
gepubliceerd op GitHub Pages). Bandleden sturen wijzigingsverzoeken in gewone taal;
jij voert ze uit als exacte zoek/vervang-paren op de HTML.

Regels:
- Elke "zoek"-string moet EXACT en PRECIES ÉÉN KEER in de HTML voorkomen,
  inclusief witruimte en inspringen. Neem genoeg context op om uniek te zijn.
- De grote foto's zijn vervangen door placeholders zoals base64,__IMG_3__.
  Laat die placeholders intact, tenzij expliciet gevraagd wordt een foto te
  verwijderen of verplaatsen.
- De setlist staat in een JavaScript-array; wijzig die alleen als er expliciet
  om gevraagd wordt (setlist-wijzigingen lopen normaal via aparte commando's).
- Nieuwe foto's staan al in de repo onder het opgegeven pad; verwijs ernaar met
  <img src="images/...."> en match de stijl/opmaak van vergelijkbare elementen.
- Kun je een verzoek niet (veilig) uitvoeren, zet het dan in "niet_uitgevoerd"
  met een korte reden, en sla het over.
- "samenvatting" is voor de bandleden in Telegram: kort, in het Nederlands,
  per verzoek één regel met wat er veranderd is."""

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "zoek":    {"type": "string"},
                    "vervang": {"type": "string"},
                },
                "required": ["zoek", "vervang"],
                "additionalProperties": False,
            },
        },
        "samenvatting":    {"type": "string"},
        "niet_uitgevoerd": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["edits", "samenvatting", "niet_uitgevoerd"],
    "additionalProperties": False,
}


def claude_edit(stripped_html, changes):
    requests_text = []
    for c in changes:
        line = f"#{c['id']} ({c['user']}, {c['tijd']}): {c['verzoek']}"
        if c.get('foto'):
            line += f"\n   [Bijbehorende foto staat in de repo op: {c['foto']}]"
        requests_text.append(line)

    client = anthropic.Anthropic()  # leest ANTHROPIC_API_KEY op aanroep-moment
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        system=CLAUDE_SYSTEM,
        tools=[{
            "name": "html_edits",
            "description": "Geef de HTML-bewerkingen terug als zoek/vervang-paren",
            "input_schema": EDIT_SCHEMA,
        }],
        tool_choice={"type": "tool", "name": "html_edits"},
        messages=[{
            "role": "user",
            "content": (
                "Wijzigingsverzoeken van de bandleden:\n\n"
                + "\n\n".join(requests_text)
                + "\n\n--- HUIDIGE HTML (foto's vervangen door placeholders) ---\n\n"
                + stripped_html
            ),
        }],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    return tool_use.input


def apply_edits(html, edits):
    ok, failed = 0, []
    for e in edits:
        if html.count(e['zoek']) == 1:
            html = html.replace(e['zoek'], e['vervang'])
            ok += 1
        else:
            failed.append(e)
    return html, ok, failed


# ── Directe verwerking ─────────────────────────────────────────────────────────

async def verwerk_direct(update: Update, naam: str, verzoek: str, foto_pad: str = None):
    """Verwerk een wijzigingsverzoek direct via Claude en zet het meteen live."""
    msg = await update.message.reply_text("Bezig met wijziging doorvoeren… ⏳")
    try:
        html = await asyncio.to_thread(get_html)
        stripped, blobs = strip_images(html)

        change = {
            'id': 1,
            'user': naam,
            'tijd': datetime.datetime.now(TZ).strftime('%Y-%m-%d %H:%M'),
            'verzoek': verzoek,
            'foto': foto_pad,
        }

        result = await asyncio.to_thread(claude_edit, stripped, [change])
        new_html, ok, failed = apply_edits(stripped, result['edits'])

        placeholders_ok = all(f'__IMG_{i}__' in new_html for i in range(len(blobs)))

        if ok > 0 and placeholders_ok and '</html>' in new_html:
            final_html = restore_images(new_html, blobs)
            commit_msg = f"Bot: {verzoek[:72]}"
            await asyncio.to_thread(commit_html, final_html, commit_msg)
            tekst = (
                f"✅ Gedaan!\n{result['samenvatting']}\n\n"
                f"Website is over ~1 minuut bijgewerkt 🎸"
            )
        elif not result['edits']:
            tekst = (
                "⚠️ Claude kon dit verzoek niet omzetten naar een wijziging. "
                "Probeer het anders te omschrijven."
            )
        elif ok == 0:
            tekst = (
                "❌ De te vervangen tekst werd niet (uniek) gevonden in de HTML. "
                "Probeer het verzoek specifieker te omschrijven."
            )
        else:
            tekst = "❌ Wijziging niet doorgevoerd — de HTML zou beschadigd raken."

        if result.get('niet_uitgevoerd'):
            tekst += "\n⚠️ Overgeslagen: " + "; ".join(result['niet_uitgevoerd'])

        await msg.edit_text(tekst)

    except Exception as e:
        logger.exception("Verwerking mislukt")
        await msg.edit_text(f"❌ Fout bij verwerken: {e}")


# ── Telegram handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"Hoi {name}! \U0001f3b8\n\n"
        f"Jouw Telegram ID: {uid}\n\n"
        f"Stuur dit nummer naar Kobus om toegang te krijgen.\n\n"
        f"Setlist (direct live):\n"
        f"/setlist – toon de setlist\n"
        f"/toevoegen Artiest | Titel | Decennium\n"
        f"/verwijderen Titel\n\n"
        f"Website aanpassen (ook direct live):\n"
        f"Stuur gewoon een berichtje met wat je veranderd wilt hebben,\n"
        f"of stuur een foto met als bijschrift waar hij moet komen.\n\n"
        f"/help – meer uitleg"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "\U0001f3b8 Drive Website Bot\n\n"
        "SETLIST (direct live):\n"
        "/setlist – toon huidige setlist\n"
        "/toevoegen Artiest | Titel | Decennium\n"
        f"  Decennia: {', '.join(VALID_DECADES)}\n"
        "  Bijv: /toevoegen AC/DC | Highway to Hell | 70s\n"
        "/verwijderen Titel\n\n"
        "WEBSITE AANPASSEN (direct live):\n"
        "• Stuur een gewoon berichtje in de groep of privé, bijv:\n"
        "  \"Verander de intro-tekst naar: ...\"\n"
        "  \"Voeg onder Foto's een kop 'Optredens' toe\"\n"
        "• Stuur een foto met bijschrift, bijv:\n"
        "  \"Zet deze foto bij de andere bandfoto's\"\n\n"
        "Claude verwerkt de wijziging direct — website is binnen ~1 min bijgewerkt.\n\n"
        "/chatid – toon het chat-id (voor de groep-setup)"
    )


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Chat-ID: {update.effective_chat.id}")


async def cmd_setlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        await update.message.reply_text(f"Geen toegang. Jouw ID: {update.effective_user.id}")
        return
    msg = await update.message.reply_text("Setlist ophalen… ⏳")
    try:
        html = await asyncio.to_thread(get_html)
        text = setlist_text(html)
        await msg.delete()
        for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            await update.message.reply_text(chunk)
    except Exception as e:
        await msg.edit_text(f"❌ Fout: {e}")


async def cmd_toevoegen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        await update.message.reply_text(f"Geen toegang. Jouw ID: {update.effective_user.id}")
        return
    raw   = ' '.join(context.args) if context.args else ''
    parts = [p.strip() for p in raw.split('|')]
    if len(parts) != 3:
        await update.message.reply_text(
            "Gebruik: /toevoegen Artiest | Titel | Decennium\n"
            f"Decennia: {', '.join(VALID_DECADES)}\n"
            "Bijv: /toevoegen AC/DC | Highway to Hell | 70s"
        )
        return
    artist, title, decade = parts
    if decade not in VALID_DECADES:
        await update.message.reply_text(f"Ongeldig decennium. Kies uit: {', '.join(VALID_DECADES)}")
        return
    msg = await update.message.reply_text(f"Toevoegen: {title} – {artist}… ⏳")
    try:
        html     = await asyncio.to_thread(get_html)
        new_html = add_song(html, artist, title, decade)
        count    = song_count(new_html)
        await asyncio.to_thread(commit_html, new_html, f"Bot: voeg '{title}' ({artist}) toe")
        await msg.edit_text(
            f"✅ {title} – {artist} toegevoegd!\n"
            f"Setlist telt nu {count} nummers.\n"
            f"Website wordt over ~1 min bijgewerkt."
        )
    except Exception as e:
        await msg.edit_text(f"❌ Fout: {e}")


async def cmd_verwijderen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        await update.message.reply_text(f"Geen toegang. Jouw ID: {update.effective_user.id}")
        return
    title = ' '.join(context.args).strip() if context.args else ''
    if not title:
        await update.message.reply_text(
            "Gebruik: /verwijderen Titel\n"
            "Bijv: /verwijderen Highway to Hell"
        )
        return
    msg = await update.message.reply_text(f"Verwijderen: {title}… ⏳")
    try:
        html             = await asyncio.to_thread(get_html)
        new_html, count  = remove_song(html, title)
        if new_html is None:
            await msg.edit_text(
                f"❌ '{title}' niet gevonden.\n"
                "Gebruik /setlist om de exacte titel te zien."
            )
            return
        await asyncio.to_thread(commit_html, new_html, f"Bot: verwijder '{title}'")
        await msg.edit_text(
            f"✅ {title} verwijderd!\n"
            f"Setlist telt nu {count} nummers.\n"
            f"Website wordt over ~1 min bijgewerkt."
        )
    except Exception as e:
        await msg.edit_text(f"❌ Fout: {e}")


async def vrije_tekst(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Vrije tekst (privé of groep) = directe wijziging van de website via Claude."""
    if update.effective_user.id not in ALLOWED_USERS:
        # In privéchat feedback geven; in groep stilletjes negeren
        if update.effective_chat.type == 'private':
            await update.message.reply_text(
                f"Geen toegang. Jouw ID: {update.effective_user.id}\n"
                "Stuur dit nummer naar Kobus."
            )
        return

    tekst = update.message.text.strip()
    naam  = update.effective_user.first_name
    await verwerk_direct(update, naam, tekst)


async def foto_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Foto met bijschrift: direct opslaan en op de site zetten."""
    if update.effective_user.id not in ALLOWED_USERS:
        if update.effective_chat.type == 'private':
            await update.message.reply_text(f"Geen toegang. Jouw ID: {update.effective_user.id}")
        return

    caption = (update.message.caption or '').strip()
    if not caption:
        await update.message.reply_text(
            "Stuur de foto nog eens mét een bijschrift waarin staat waar hij "
            "moet komen, bijv: \"Zet deze foto bij de andere bandfoto's\"."
        )
        return

    msg = await update.message.reply_text("Foto opslaan… ⏳")
    try:
        photo   = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        data    = bytes(await tg_file.download_as_bytearray())

        ts   = datetime.datetime.now(TZ).strftime('%Y%m%d_%H%M%S')
        path = f'images/foto_{ts}.jpg'
        naam = update.effective_user.first_name
        await asyncio.to_thread(commit_files, {path: data}, f"Bot: upload {path}")
        await msg.edit_text("Foto opgeslagen, wijziging doorvoeren… ⏳")
        await verwerk_direct(update, naam, caption, path)
    except Exception as e:
        await msg.edit_text(f"❌ Fout bij opslaan van de foto: {e}")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler('start',       cmd_start))
    app.add_handler(CommandHandler('help',        cmd_help))
    app.add_handler(CommandHandler('chatid',      cmd_chatid))
    app.add_handler(CommandHandler('setlist',     cmd_setlist))
    app.add_handler(CommandHandler('toevoegen',   cmd_toevoegen))
    app.add_handler(CommandHandler('verwijderen', cmd_verwijderen))

    # Vrije tekst: privéchat én de bandgroep
    chat_filter = filters.ChatType.PRIVATE
    if GROUP_CHAT_ID:
        chat_filter = chat_filter | filters.Chat(chat_id=int(GROUP_CHAT_ID))

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & chat_filter,
        vrije_tekst
    ))
    app.add_handler(MessageHandler(
        filters.PHOTO & chat_filter,
        foto_handler
    ))

    logger.info("Drive bot gestart \U0001f3b8 (directe verwerking via Claude)")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()

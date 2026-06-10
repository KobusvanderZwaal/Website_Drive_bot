#!/usr/bin/env python3
"""
Drive Band Website Bot
Hiermee kunnen bandleden de setlist op de website aanpassen via Telegram.
"""

import os
import re
import base64
import logging
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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

REPO_OWNER = 'KobusvanderZwaal'
REPO_NAME  = 'Website_Drive'
FILE_PATH  = 'index.html'
BRANCH     = 'main'

API        = 'https://api.github.com'
GH_HEADERS = {
    'Authorization': f'token {GITHUB_TOKEN}',
    'Accept': 'application/vnd.github.v3+json'
}

VALID_DECADES = ['60s', '70s', '80s', '90s', '00s', '10s', '20s']


# ── GitHub hulpfuncties ────────────────────────────────────────────────────────

def get_html():
    """Haal de ruwe HTML op van GitHub."""
    url = f'https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{BRANCH}/{FILE_PATH}'
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def commit_html(html, commit_msg):
    """Sla bijgewerkte HTML op via de Git Data API (werkt ook voor bestanden > 1 MB)."""
    # 1. Huidige commit SHA ophalen
    ref_url  = f'{API}/repos/{REPO_OWNER}/{REPO_NAME}/git/refs/heads/{BRANCH}'
    ref_data = requests.get(ref_url, headers=GH_HEADERS, timeout=30)
    ref_data.raise_for_status()
    latest_sha = ref_data.json()['object']['sha']

    # 2. Tree SHA van die commit ophalen
    commit_data = requests.get(
        f'{API}/repos/{REPO_OWNER}/{REPO_NAME}/git/commits/{latest_sha}',
        headers=GH_HEADERS, timeout=30
    )
    commit_data.raise_for_status()
    base_tree = commit_data.json()['tree']['sha']

    # 3. Blob aanmaken met nieuwe inhoud
    blob = requests.post(
        f'{API}/repos/{REPO_OWNER}/{REPO_NAME}/git/blobs',
        headers=GH_HEADERS,
        json={'content': base64.b64encode(html.encode('utf-8')).decode('utf-8'),
              'encoding': 'base64'},
        timeout=60
    )
    blob.raise_for_status()

    # 4. Nieuwe tree aanmaken
    tree = requests.post(
        f'{API}/repos/{REPO_OWNER}/{REPO_NAME}/git/trees',
        headers=GH_HEADERS,
        json={
            'base_tree': base_tree,
            'tree': [{'path': FILE_PATH, 'mode': '100644',
                      'type': 'blob', 'sha': blob.json()['sha']}]
        },
        timeout=30
    )
    tree.raise_for_status()

    # 5. Nieuwe commit aanmaken
    new_commit = requests.post(
        f'{API}/repos/{REPO_OWNER}/{REPO_NAME}/git/commits',
        headers=GH_HEADERS,
        json={'message': commit_msg, 'tree': tree.json()['sha'],
              'parents': [latest_sha]},
        timeout=30
    )
    new_commit.raise_for_status()

    # 6. Branch verwijzen naar nieuwe commit
    result = requests.patch(
        ref_url,
        headers=GH_HEADERS,
        json={'sha': new_commit.json()['sha']},
        timeout=30
    )
    result.raise_for_status()


def song_count(html):
    return len(re.findall(r"\{ decade:", html))


def update_count(html, count):
    return re.sub(r'\d+ nummers', f'{count} nummers', html)


def add_song(html, artist, title, decade):
    """Voeg een nummer toe aan het juiste decennium."""
    new_entry = f"            {{ decade: '{decade}', title: \"{title}\", artist: '{artist}' }},\n"

    positions = [m.start() for m in re.finditer(rf"decade: '{decade}'", html)]
    if positions:
        line_end = html.find('\n', positions[-1])
        html = html[:line_end + 1] + new_entry + html[line_end + 1:]
    else:
        # Decennium bestaat nog niet: vlak voor sluit-]; van de array
        close = html.rfind('];')
        line_start = html.rfind('\n', 0, close)
        html = html[:line_start + 1] + new_entry + html[line_start + 1:]

    return update_count(html, song_count(html))


def remove_song(html, title):
    """Verwijder een nummer. Geeft (None, 0) terug als niet gevonden."""
    pattern = rf'[ \t]*\{{[^}}]*title: ["\']?{re.escape(title)}["\']?[^}}]*\}}[,]?\n'
    new_html, n = re.subn(pattern, '', html, flags=re.IGNORECASE)
    if n == 0:
        return None, 0
    count = song_count(new_html)
    return update_count(new_html, count), count


def setlist_text(html):
    """Maak een leesbare tekst van de huidige setlist."""
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


# ── Telegram handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"Hoi {name}! \U0001f3b8\n\n"
        f"Jouw Telegram ID: {uid}\n\n"
        f"Stuur dit nummer naar Kobus om toegang te krijgen.\n\n"
        f"Beschikbare commando's:\n"
        f"/setlist – toon de setlist\n"
        f"/toevoegen Artiest | Titel | Decennium\n"
        f"/verwijderen Titel\n"
        f"/help – meer uitleg"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "\U0001f3b8 Drive Website Bot\n\n"
        "/setlist – toon huidige setlist\n\n"
        "/toevoegen Artiest | Titel | Decennium\n"
        f"  Decennia: {', '.join(VALID_DECADES)}\n"
        "  Bijv: /toevoegen AC/DC | Highway to Hell | 70s\n\n"
        "/verwijderen Titel\n"
        "  Bijv: /verwijderen Highway to Hell\n"
    )


async def cmd_setlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user.id in ALLOWED_USERS:
        await update.message.reply_text(f"Geen toegang. Jouw ID: {update.effective_user.id}")
        return

    msg = await update.message.reply_text("Setlist ophalen… ⏳")
    try:
        html = get_html()
        text = setlist_text(html)
        await msg.delete()
        for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            await update.message.reply_text(chunk)
    except Exception as e:
        await msg.edit_text(f"❌ Fout: {e}")


async def cmd_toevoegen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user.id in ALLOWED_USERS:
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
        html     = get_html()
        new_html = add_song(html, artist, title, decade)
        count    = song_count(new_html)
        commit_html(new_html, f"Bot: voeg '{title}' ({artist}) toe")
        await msg.edit_text(
            f"✅ {title} – {artist} toegevoegd!\n"
            f"Setlist telt nu {count} nummers.\n"
            f"Website wordt over ~1 min bijgewerkt."
        )
    except Exception as e:
        await msg.edit_text(f"❌ Fout: {e}")


async def cmd_verwijderen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user.id in ALLOWED_USERS:
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
        html             = get_html()
        new_html, count  = remove_song(html, title)
        if new_html is None:
            await msg.edit_text(
                f"❌ '{title}' niet gevonden.\n"
                "Gebruik /setlist om de exacte titel te zien."
            )
            return
        commit_html(new_html, f"Bot: verwijder '{title}'")
        await msg.edit_text(
            f"✅ {title} verwijderd!\n"
            f"Setlist telt nu {count} nummers.\n"
            f"Website wordt over ~1 min bijgewerkt."
        )
    except Exception as e:
        await msg.edit_text(f"❌ Fout: {e}")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start',      cmd_start))
    app.add_handler(CommandHandler('help',       cmd_help))
    app.add_handler(CommandHandler('setlist',    cmd_setlist))
    app.add_handler(CommandHandler('toevoegen',  cmd_toevoegen))
    app.add_handler(CommandHandler('verwijderen', cmd_verwijderen))

    logger.info("Drive bot gestart \U0001f3b8")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()

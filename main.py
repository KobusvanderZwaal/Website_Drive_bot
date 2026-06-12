#!/usr/bin/env python3
"""
Drive Band Website Bot
Hiermee kunnen bandleden de setlist en de rest van de website aanpassen via Telegram.

Setlist-commando's (/toevoegen, /verwijderen) werken direct.
Vrije-tekst wijzigingen en foto's gaan in een wachtrij en worden elke avond
in één keer doorgevoerd door Claude, met een samenvatting naar de bandgroep.
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
GROUP_CHAT_ID  = os.environ.get('GROUP_CHAT_ID', '').strip()   # chat-id van de bandgroep
PUBLISH_HOUR   = int(os.environ.get('PUBLISH_HOUR', '22'))     # uur waarop wijzigingen live gaan
CLAUDE_MODEL   = os.environ.get('CLAUDE_MODEL', 'claude-opus-4-8')

REPO_OWNER = 'KobusvanderZwaal'
REPO_NAME  = 'Website_Drive'
BOT_REPO   = 'Website_Drive_bot'
FILE_PATH  = 'index.html'
QUEUE_PATH = 'pending_changes.json'
BRANCH     = 'main'

API        = 'https://api.github.com'
GH_HEADERS = {
    'Authorization': f'token {GITHUB_TOKEN}',
    'Accept': 'application/vnd.github.v3+json'
}

TZ = pytz.timezone('Europe/Amsterdam')

VALID_DECADES = ['60s', '70s', '80s', '90s', '00s', '10s', '20s']

claude = anthropic.Anthropic()  # leest ANTHROPIC_API_KEY uit de omgeving


# ── GitHub hulpfuncties ────────────────────────────────────────────────────────

def get_html():
    """Haal de ruwe HTML op van GitHub."""
    url = f'https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{BRANCH}/{FILE_PATH}'
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def commit_files(files, commit_msg):
    """Commit één of meer bestanden (pad -> bytes) via de Git Data API.
    Werkt ook voor bestanden > 1 MB en voor binaire bestanden zoals foto's."""
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

    # 3. Blob per bestand aanmaken
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

    # 4. Nieuwe tree aanmaken
    tree = requests.post(
        f'{API}/repos/{REPO_OWNER}/{REPO_NAME}/git/trees',
        headers=GH_HEADERS,
        json={'base_tree': base_tree, 'tree': tree_entries},
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


def commit_html(html, commit_msg):
    commit_files({FILE_PATH: html.encode('utf-8')}, commit_msg)


# ── Wachtrij (opgeslagen in de bot-repo, overleeft herstarts) ─────────────────

def load_queue():
    """Geeft (queue_dict, sha) terug. sha is None als het bestand nog niet bestaat."""
    url = f'{API}/repos/{REPO_OWNER}/{BOT_REPO}/contents/{QUEUE_PATH}'
    r = requests.get(url, headers=GH_HEADERS, timeout=30)
    if r.status_code == 404:
        return {'changes': [], 'next_id': 1}, None
    r.raise_for_status()
    data = r.json()
    queue = json.loads(base64.b64decode(data['content']).decode('utf-8'))
    return queue, data['sha']


def save_queue(queue, sha):
    url = f'{API}/repos/{REPO_OWNER}/{BOT_REPO}/contents/{QUEUE_PATH}'
    body = {
        'message': 'Bot: wachtrij bijgewerkt',
        'content': base64.b64encode(
            json.dumps(queue, ensure_ascii=False, indent=1).encode('utf-8')
        ).decode('utf-8'),
    }
    if sha:
        body['sha'] = sha
    r = requests.put(url, headers=GH_HEADERS, json=body, timeout=30)
    r.raise_for_status()


def add_to_queue(user_name, request_text, image_path=None):
    queue, sha = load_queue()
    queue['changes'].append({
        'id': queue['next_id'],
        'user': user_name,
        'tijd': datetime.datetime.now(TZ).strftime('%Y-%m-%d %H:%M'),
        'verzoek': request_text,
        'foto': image_path,
    })
    queue['next_id'] += 1
    save_queue(queue, sha)
    return queue['changes'][-1]['id']


# ── Setlist-functies (ongewijzigd) ─────────────────────────────────────────────

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


# ── AI-bewerking van de website ────────────────────────────────────────────────

IMG_RE = re.compile(r'base64,[A-Za-z0-9+/=\s]{100,}')

def strip_images(html):
    """Vervang grote base64-foto's door placeholders zodat de HTML klein genoeg
    is voor Claude. Geeft (gestripte_html, lijst_met_blobs) terug."""
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
    """Vraag Claude om de wijzigingsverzoeken om te zetten in zoek/vervang-paren."""
    requests_text = []
    for c in changes:
        line = f"#{c['id']} ({c['user']}, {c['tijd']}): {c['verzoek']}"
        if c.get('foto'):
            line += f"\n   [Bijbehorende foto staat in de repo op: {c['foto']}]"
        requests_text.append(line)

    response = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"format": {"type": "json_schema", "schema": EDIT_SCHEMA}},
        system=CLAUDE_SYSTEM,
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
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def apply_edits(html, edits):
    """Pas zoek/vervang-paren toe. Geeft (nieuwe_html, gelukt, mislukt) terug."""
    ok, failed = 0, []
    for e in edits:
        if html.count(e['zoek']) == 1:
            html = html.replace(e['zoek'], e['vervang'])
            ok += 1
        else:
            failed.append(e)
    return html, ok, failed


def setlist_commits_today():
    """Setlist-commits van vandaag (voor in de avondsamenvatting)."""
    since = datetime.datetime.now(TZ).replace(hour=0, minute=0, second=0).isoformat()
    r = requests.get(
        f'{API}/repos/{REPO_OWNER}/{REPO_NAME}/commits',
        headers=GH_HEADERS, params={'since': since}, timeout=30
    )
    r.raise_for_status()
    msgs = [c['commit']['message'] for c in r.json()]
    return [m for m in msgs if m.startswith('Bot: voeg') or m.startswith('Bot: verwijder')]


async def publiceer(bot, chat_id_feedback=None):
    """Voer alle wachtende wijzigingen door en stuur een samenvatting.
    chat_id_feedback: extra chat die voortgangsberichten krijgt (bij /publiceer)."""
    queue, sha = load_queue()
    changes = queue['changes']
    setlist_done = await asyncio.to_thread(setlist_commits_today)

    if not changes and not setlist_done:
        if chat_id_feedback:
            await bot.send_message(chat_id_feedback, "De wachtrij is leeg — niets te publiceren.")
        return

    lines = [f"\U0001f3b8 Drive website-update {datetime.datetime.now(TZ).strftime('%d-%m-%Y')}\n"]

    if changes:
        if chat_id_feedback:
            await bot.send_message(chat_id_feedback,
                                   f"Bezig met {len(changes)} wijziging(en)… ⏳")
        try:
            html = await asyncio.to_thread(get_html)
            stripped, blobs = strip_images(html)
            result = await asyncio.to_thread(claude_edit, stripped, changes)
            new_html, ok, failed = apply_edits(stripped, result['edits'])

            # Veiligheidschecks voordat we committen
            placeholders_ok = all(f'__IMG_{i}__' in new_html for i in range(len(blobs)))
            if ok > 0 and placeholders_ok and '</html>' in new_html:
                final_html = restore_images(new_html, blobs)
                await asyncio.to_thread(
                    commit_html, final_html,
                    f"Bot: dagelijkse update ({ok} wijziging(en))"
                )
                lines.append(result['samenvatting'])
                queue['changes'] = []
                save_queue(queue, sha)
            elif ok == 0:
                lines.append("❌ Geen van de wijzigingen kon worden toegepast — "
                             "de verzoeken blijven in de wachtrij staan.")
            else:
                lines.append("❌ De wijzigingen zijn uit veiligheid NIET doorgevoerd "
                             "(de HTML zou beschadigd raken). Verzoeken blijven in de wachtrij.")

            for e in failed:
                lines.append(f"⚠️ Niet toegepast (tekst niet uniek gevonden): {e['zoek'][:60]}…")
            for reason in result.get('niet_uitgevoerd', []):
                lines.append(f"⚠️ Overgeslagen: {reason}")

        except Exception as e:
            logger.exception("Publicatie mislukt")
            lines.append(f"❌ Fout bij doorvoeren van de wijzigingen: {e}\n"
                         "De verzoeken blijven in de wachtrij staan.")

    if setlist_done:
        lines.append("\nSetlist-wijzigingen van vandaag (al live):")
        for m in setlist_done:
            lines.append(f"  • {m.removeprefix('Bot: ')}")

    if changes:
        lines.append("\nDe website is over ±1 minuut bijgewerkt: "
                     f"https://{REPO_OWNER.lower()}.github.io/{REPO_NAME}/")

    summary = '\n'.join(lines)
    targets = []
    if GROUP_CHAT_ID:
        targets.append(GROUP_CHAT_ID)
    elif ALLOWED_USERS:
        targets.extend(ALLOWED_USERS)
    if chat_id_feedback and str(chat_id_feedback) not in [str(t) for t in targets]:
        targets.append(chat_id_feedback)

    for t in targets:
        try:
            await bot.send_message(t, summary)
        except Exception:
            logger.exception(f"Kon samenvatting niet sturen naar {t}")


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
        f"Website aanpassen (gaat 's avonds live):\n"
        f"Stuur gewoon een berichtje met wat je veranderd wilt hebben,\n"
        f"of stuur een foto met als bijschrift waar hij moet komen.\n"
        f"/wachtrij – toon wat er klaarstaat\n"
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
        "WEBSITE AANPASSEN (verzameld, elke avond om "
        f"{PUBLISH_HOUR}:00 in één keer live):\n"
        "• Stuur een gewoon berichtje, bijv:\n"
        "  \"Verander de intro-tekst naar: ...\"\n"
        "  \"Voeg onder Foto's een kop 'Optredens' toe\"\n"
        "• Stuur een foto met bijschrift, bijv:\n"
        "  \"Zet deze foto bij de andere bandfoto's\"\n\n"
        "/wachtrij – toon wachtende wijzigingen\n"
        "/annuleer nummer – haal een wijziging uit de wachtrij\n"
        "/publiceer – voer de wachtrij nu direct door\n"
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


async def cmd_wachtrij(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        await update.message.reply_text(f"Geen toegang. Jouw ID: {update.effective_user.id}")
        return

    queue, _ = await asyncio.to_thread(load_queue)
    if not queue['changes']:
        await update.message.reply_text(
            f"De wachtrij is leeg. Wijzigingen gaan om {PUBLISH_HOUR}:00 live."
        )
        return

    lines = [f"Wachtende wijzigingen (gaan om {PUBLISH_HOUR}:00 live):\n"]
    for c in queue['changes']:
        foto = " \U0001f4f7" if c.get('foto') else ""
        lines.append(f"#{c['id']} ({c['user']}){foto}: {c['verzoek']}")
    lines.append("\n/annuleer nummer – verwijderen uit wachtrij")
    await update.message.reply_text('\n'.join(lines))


async def cmd_annuleer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        await update.message.reply_text(f"Geen toegang. Jouw ID: {update.effective_user.id}")
        return

    try:
        nr = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Gebruik: /annuleer nummer (zie /wachtrij)")
        return

    queue, sha = await asyncio.to_thread(load_queue)
    before = len(queue['changes'])
    queue['changes'] = [c for c in queue['changes'] if c['id'] != nr]
    if len(queue['changes']) == before:
        await update.message.reply_text(f"#{nr} niet gevonden in de wachtrij.")
        return
    await asyncio.to_thread(save_queue, queue, sha)
    await update.message.reply_text(f"✅ #{nr} uit de wachtrij gehaald.")


async def cmd_publiceer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        await update.message.reply_text(f"Geen toegang. Jouw ID: {update.effective_user.id}")
        return
    await publiceer(context.bot, chat_id_feedback=update.effective_chat.id)


async def vrije_tekst(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Vrije tekst in privéchat = wijzigingsverzoek voor de website."""
    if update.effective_user.id not in ALLOWED_USERS:
        await update.message.reply_text(
            f"Geen toegang. Jouw ID: {update.effective_user.id}\n"
            "Stuur dit nummer naar Kobus."
        )
        return

    tekst = update.message.text.strip()
    naam  = update.effective_user.first_name
    nr = await asyncio.to_thread(add_to_queue, naam, tekst)
    await update.message.reply_text(
        f"✅ Genoteerd als #{nr}! Gaat vanavond om {PUBLISH_HOUR}:00 live.\n"
        "/wachtrij om alles te zien, /publiceer om direct door te voeren."
    )


async def foto_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Foto in privéchat: opslaan in de repo en (met bijschrift) in de wachtrij."""
    if update.effective_user.id not in ALLOWED_USERS:
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
        photo = update.message.photo[-1]                 # grootste versie
        tg_file = await context.bot.get_file(photo.file_id)
        data = bytes(await tg_file.download_as_bytearray())

        ts = datetime.datetime.now(TZ).strftime('%Y%m%d_%H%M%S')
        path = f'images/foto_{ts}.jpg'
        naam = update.effective_user.first_name
        await asyncio.to_thread(commit_files, {path: data}, f"Bot: upload {path}")

        nr = await asyncio.to_thread(add_to_queue, naam, caption, path)
        await msg.edit_text(
            f"✅ Foto opgeslagen en genoteerd als #{nr}!\n"
            f"Komt vanavond om {PUBLISH_HOUR}:00 op de site."
        )
    except Exception as e:
        await msg.edit_text(f"❌ Fout bij opslaan van de foto: {e}")


async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    await publiceer(context.bot)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start',       cmd_start))
    app.add_handler(CommandHandler('help',        cmd_help))
    app.add_handler(CommandHandler('chatid',      cmd_chatid))
    app.add_handler(CommandHandler('setlist',     cmd_setlist))
    app.add_handler(CommandHandler('toevoegen',   cmd_toevoegen))
    app.add_handler(CommandHandler('verwijderen', cmd_verwijderen))
    app.add_handler(CommandHandler('wachtrij',    cmd_wachtrij))
    app.add_handler(CommandHandler('annuleer',    cmd_annuleer))
    app.add_handler(CommandHandler('publiceer',   cmd_publiceer))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, vrije_tekst))
    app.add_handler(MessageHandler(
        filters.PHOTO & filters.ChatType.PRIVATE, foto_handler))

    app.job_queue.run_daily(
        daily_job,
        time=datetime.time(hour=PUBLISH_HOUR, minute=0, tzinfo=TZ),
    )

    logger.info("Drive bot gestart \U0001f3b8 (publicatie dagelijks om %d:00)", PUBLISH_HOUR)
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()

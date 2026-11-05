"""English and Latin American Spanish strings, plus a light language detector."""

from __future__ import annotations

import re
import unicodedata

from smarrtbot.util import human_bytes

_ES_WORDS = {
    "hola",
    "quiero",
    "pelicula",
    "peliculas",
    "serie",
    "series",
    "temporada",
    "temporadas",
    "borrar",
    "borra",
    "elimina",
    "eliminar",
    "agrega",
    "agregar",
    "agregame",
    "busca",
    "buscar",
    "descarga",
    "descargar",
    "baja",
    "bajar",
    "cuanto",
    "cuanta",
    "espacio",
    "queda",
    "libre",
    "disco",
    "gracias",
    "por",
    "favor",
    "que",
    "como",
    "donde",
    "cual",
    "puedes",
    "podes",
    "necesito",
    "liberar",
    "huerfanos",
    "muestrame",
    "dame",
    "de",
    "la",
    "el",
    "los",
    "las",
    "una",
    "un",
    "y",
    "para",
    "con",
    "mi",
    "esta",
    "estan",
    "hay",
    "tengo",
    "ver",
    "quiere",
}
_EN_WORDS = {
    "hi",
    "hello",
    "the",
    "add",
    "please",
    "movie",
    "movies",
    "show",
    "shows",
    "season",
    "delete",
    "remove",
    "how",
    "much",
    "space",
    "left",
    "free",
    "what",
    "can",
    "find",
    "search",
    "download",
    "get",
    "me",
    "my",
    "is",
    "are",
    "of",
    "and",
    "to",
    "orphans",
    "thanks",
    "want",
}


def _fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def detect_lang(text: str, fallback: str = "en") -> str:
    if not text:
        return fallback
    if re.search(r"[¿¡ñÑ]", text):
        return "es"
    words = re.findall(r"[a-z]+", _fold(text))
    es = sum(w in _ES_WORDS for w in words) + (1 if re.search(r"[áéíóú]", text.lower()) else 0)
    en = sum(w in _EN_WORDS for w in words)
    if es > en:
        return "es"
    if en > es:
        return "en"
    return fallback


STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "help": (
            "<b>smarrtbot</b>: ask me for movies and shows, or about disk space.\n"
            "Examples: <i>add Dune Part Two</i>, <i>how much space is left?</i>, "
            "<i>what can I delete to free 200 GB?</i>\n\n"
            "/status – services and AI state\n/queue – downloads\n/storage – disk usage\n"
            "/cleanup – cleanup candidates\n/delete &lt;title&gt; [season N] – delete a title\n"
            "/models – AI models and keys\n/whoami – your id / ask for access\n"
            "/forget – clear this chat's memory\n\n"
            "In groups, mention me or reply to me. Changes always need a tap on the button by "
            "the person who asked. {dry}\n\n"
            "<b>Privacy</b>: messages are processed by Google Gemini on its free tier; Google "
            "may use them to improve its products. Using several free keys may breach Google's "
            "terms; the owner accepts that risk. Don't send personal data."
        ),
        "dry": "⚠️ DRY RUN is ON: nothing is changed for real.",
        "not_allowed": "You are not approved yet. Send /whoami to request access.",
        "whoami": "Your user id: <code>{uid}</code>\nThis chat id: <code>{cid}</code>",
        "whoami_requested": "Access requested. The owner has been asked to approve you.",
        "whoami_approved": "You are approved. ✅",
        "whoami_wait": "Your request is already waiting for the owner.",
        "approve_req": "Access request from {name} (id <code>{uid}</code>) in {chat}.",
        "approve_btn": "✅ Approve",
        "deny_btn": "⛔ Deny",
        "approved_user": "Approved {uid}.",
        "denied_user": "Denied {uid}.",
        "you_were_approved": "You now have access to smarrtbot. Send /help.",
        "revoked": "Revoked {uid}.",
        "owner_only": "Owner only.",
        "ai_busy": "AI busy, try /help or later.",
        "no_reply": "Done.",
        "confirm_hint": "Tap to confirm (only {name} can confirm; expires in {mins} min):",
        "cancel_btn": "✖ Cancel",
        "cancelled": "Cancelled.",
        "expired": "This button has expired. Please ask again.",
        "used": "Already handled.",
        "not_yours": "Only the person who asked can confirm this.",
        "unknown": "Unknown or expired action.",
        "working": "Working…",
        "failed": "Failed: {err}",
        "add_btn": "➕ Add {label}",
        "add_text": "Add {kind} <b>{label}</b> and start searching?",
        "kind.movie": "movie",
        "kind.series": "series",
        "del_btn": "🗑 Delete {size} (no undo)",
        "del_text": "Delete <b>{label}</b>?\n{details}\n⚠️ There is no undo.",
        "clean_btn": "🧹 Delete {size} (no undo)",
        "clean_text": "Clean up {label}, {size}:\n{details}\n⚠️ There is no undo.",
        "detail.media": "• Library files: {n} ({size})",
        "detail.torrent_delete": "• Torrent to delete: {size}{seeding}",
        "detail.seeding": " (still seeding, deleting anyway)",
        "detail.torrent_kept": "• Torrent shared, kept: {size} not freed",
        "detail.no_torrent": "• No torrent found: 0 GB freed from torrents",
        "detail.no_hardlink_check": "• Hardlink check unavailable: freed size is an estimate",
        "detail.item": "• [{cat}] {label} ({size})",
        "del_done": (
            "{prefix}Deleted <b>{label}</b>.\nFreed: {freed} (expected {expected}).{extra}"
        ),
        "del_dry": "[DRY RUN] Would delete <b>{label}</b>; expected to free {expected}.{extra}",
        "del_seeding": "\nTorrent was still seeding; deleted anyway.",
        "del_kept": "\nTorrent shared, kept: {size} not freed.",
        "del_none": "\nNo torrent found, 0 GB freed from torrents.",
        "del_mismatch": "\n⚠️ The numbers don't add up: measured {freed}, expected {expected}.",
        "clean_done": "{prefix}Cleanup: {n} item(s), freed {freed}.{skipped}",
        "clean_skipped": "\nSkipped {n}: {why}",
        "pick_delete": "Which one?",
        "no_match": "No title matches “{q}”.",
        "usage_delete": "Usage: /delete &lt;title&gt; [season N]",
        "storage": (
            "<b>Disk</b>: {used} used of {total}, <b>{free} free</b> ({pct})\n"
            "Movies {movies} · TV {tv} · Torrents {torrents}{tuniq}\n{trend}"
            "<b>Largest</b>:\n{largest}"
        ),
        "storage_tuniq": " ({u} not in library)",
        "storage_trend": "Change in used space: 7d {d7}, 30d {d30}\n",
        "alert": "⚠️ Low disk space: {free} free ({pct}). Try /cleanup or /storage.",
        "weekly": "Weekly storage summary:\n",
        "cleanup_none": "Nothing to clean up in {cat}.",
        "cleanup_list": "<b>Cleanup candidates</b> ({cat}):\n{items}\n\nAsk me to delete some by name or id, e.g. “delete the orphans”.",
        "queue_empty": "No downloads in progress.",
        "status": (
            "<b>Status</b> {dry}\nRadarr: {radarr}\nSonarr: {sonarr}\nqBittorrent: {qbit}\n"
            "Media server: {ms}\nAI model: {model} · keys live: {keys}"
        ),
        "models_hdr": "<b>Models</b> (best first):",
        "keys_hdr": "<b>Keys</b>:",
        "tv_flag": "📺 <b>{label}</b> may not Direct Play on the TV:\n{issues}",
        "tv_btn": "🔁 Blocklist and re-search",
        "tv_confirm": "Blocklist this release of <b>{label}</b>, delete the file and search again?\n⚠️ There is no undo.",
        "tv_confirm_btn": "🔁 Replace (no undo)",
        "issue.dolby_vision": "• Dolby Vision (TV has no DV)",
        "issue.truehd": "• TrueHD audio",
        "issue.dts": "• DTS audio (TV can't decode DTS)",
        "issue.pgs_subs": "• Embedded PGS subtitles (forces burn-in)",
        "issue.ass_subs": "• Embedded ASS subtitles (forces burn-in)",
        "issue.bitrate": "• Bitrate {mbps} Mbps over the cap",
        "forgot": "Memory for this chat cleared.",
    },
    "es": {
        "help": (
            "<b>smarrtbot</b>: pídeme películas y series, o pregúntame por el espacio en disco.\n"
            "Ejemplos: <i>agrega Dune Parte Dos</i>, <i>¿cuánto espacio queda?</i>, "
            "<i>¿qué puedo borrar para liberar 200 GB?</i>\n\n"
            "/status – servicios y estado de la IA\n/queue – descargas\n/storage – uso del disco\n"
            "/cleanup – candidatos para limpiar\n/delete &lt;título&gt; [temporada N] – borrar\n"
            "/models – modelos y claves de IA\n/whoami – tu id / pedir acceso\n"
            "/forget – borrar la memoria de este chat\n\n"
            "En grupos, menciónome o respóndeme. Los cambios siempre necesitan que la misma "
            "persona que lo pidió toque el botón. {dry}\n\n"
            "<b>Privacidad</b>: los mensajes los procesa Google Gemini en su nivel gratuito; "
            "Google puede usarlos para mejorar sus productos. Usar varias claves gratuitas puede "
            "violar los términos de Google; el dueño acepta ese riesgo. No envíes datos personales."
        ),
        "dry": "⚠️ MODO PRUEBA activo: no se cambia nada de verdad.",
        "not_allowed": "Todavía no tienes acceso. Envía /whoami para pedirlo.",
        "whoami": "Tu id de usuario: <code>{uid}</code>\nId de este chat: <code>{cid}</code>",
        "whoami_requested": "Acceso solicitado. Le pedí al dueño que te apruebe.",
        "whoami_approved": "Ya tienes acceso. ✅",
        "whoami_wait": "Tu solicitud ya está esperando al dueño.",
        "approve_req": "Solicitud de acceso de {name} (id <code>{uid}</code>) en {chat}.",
        "approve_btn": "✅ Aprobar",
        "deny_btn": "⛔ Rechazar",
        "approved_user": "Aprobado {uid}.",
        "denied_user": "Rechazado {uid}.",
        "you_were_approved": "Ya tienes acceso a smarrtbot. Envía /help.",
        "revoked": "Acceso revocado a {uid}.",
        "owner_only": "Solo el dueño.",
        "ai_busy": "La IA está ocupada, prueba /help o más tarde.",
        "no_reply": "Listo.",
        "confirm_hint": "Toca para confirmar (solo {name} puede; vence en {mins} min):",
        "cancel_btn": "✖ Cancelar",
        "cancelled": "Cancelado.",
        "expired": "Este botón venció. Pídelo de nuevo.",
        "used": "Ya se procesó.",
        "not_yours": "Solo quien lo pidió puede confirmarlo.",
        "unknown": "Acción desconocida o vencida.",
        "working": "Procesando…",
        "failed": "Falló: {err}",
        "add_btn": "➕ Agregar {label}",
        "add_text": "¿Agregar la {kind} <b>{label}</b> y empezar a buscarla?",
        "kind.movie": "película",
        "kind.series": "serie",
        "del_btn": "🗑 Borrar {size} (sin deshacer)",
        "del_text": "¿Borrar <b>{label}</b>?\n{details}\n⚠️ No se puede deshacer.",
        "clean_btn": "🧹 Borrar {size} (sin deshacer)",
        "clean_text": "Limpiar {label}, {size}:\n{details}\n⚠️ No se puede deshacer.",
        "detail.media": "• Archivos de la biblioteca: {n} ({size})",
        "detail.torrent_delete": "• Torrent a borrar: {size}{seeding}",
        "detail.seeding": " (todavía compartiendo, se borra igual)",
        "detail.torrent_kept": "• Torrent compartido, se conserva: {size} sin liberar",
        "detail.no_torrent": "• No se encontró torrent: 0 GB liberados de torrents",
        "detail.no_hardlink_check": "• Sin verificación de hardlinks: el tamaño es estimado",
        "detail.item": "• [{cat}] {label} ({size})",
        "del_done": "{prefix}Borrado <b>{label}</b>.\nLiberado: {freed} (esperado {expected}).{extra}",
        "del_dry": "[MODO PRUEBA] Borraría <b>{label}</b>; liberaría unos {expected}.{extra}",
        "del_seeding": "\nEl torrent todavía se compartía; se borró igual.",
        "del_kept": "\nTorrent compartido, se conservó: {size} sin liberar.",
        "del_none": "\nNo se encontró torrent, 0 GB liberados de torrents.",
        "del_mismatch": "\n⚠️ Los números no cuadran: medido {freed}, esperado {expected}.",
        "clean_done": "{prefix}Limpieza: {n} elemento(s), liberado {freed}.{skipped}",
        "clean_skipped": "\nOmitidos {n}: {why}",
        "pick_delete": "¿Cuál?",
        "no_match": "Ningún título coincide con “{q}”.",
        "usage_delete": "Uso: /delete &lt;título&gt; [temporada N]",
        "storage": (
            "<b>Disco</b>: {used} usados de {total}, <b>{free} libres</b> ({pct})\n"
            "Películas {movies} · Series {tv} · Torrents {torrents}{tuniq}\n{trend}"
            "<b>Más grandes</b>:\n{largest}"
        ),
        "storage_tuniq": " ({u} fuera de la biblioteca)",
        "storage_trend": "Cambio en espacio usado: 7d {d7}, 30d {d30}\n",
        "alert": "⚠️ Poco espacio en disco: {free} libres ({pct}). Prueba /cleanup o /storage.",
        "weekly": "Resumen semanal de almacenamiento:\n",
        "cleanup_none": "No hay nada para limpiar en {cat}.",
        "cleanup_list": "<b>Candidatos para limpiar</b> ({cat}):\n{items}\n\nPídeme borrar algunos por nombre o id, p. ej. “borra los huérfanos”.",
        "queue_empty": "No hay descargas en curso.",
        "status": (
            "<b>Estado</b> {dry}\nRadarr: {radarr}\nSonarr: {sonarr}\nqBittorrent: {qbit}\n"
            "Servidor multimedia: {ms}\nModelo IA: {model} · claves activas: {keys}"
        ),
        "models_hdr": "<b>Modelos</b> (mejor primero):",
        "keys_hdr": "<b>Claves</b>:",
        "tv_flag": "📺 <b>{label}</b> podría no reproducirse directo en la TV:\n{issues}",
        "tv_btn": "🔁 Bloquear y buscar otra",
        "tv_confirm": "¿Bloquear esta versión de <b>{label}</b>, borrar el archivo y buscar otra?\n⚠️ No se puede deshacer.",
        "tv_confirm_btn": "🔁 Reemplazar (sin deshacer)",
        "issue.dolby_vision": "• Dolby Vision (la TV no tiene DV)",
        "issue.truehd": "• Audio TrueHD",
        "issue.dts": "• Audio DTS (la TV no decodifica DTS)",
        "issue.pgs_subs": "• Subtítulos PGS incrustados (obliga a transcodificar)",
        "issue.ass_subs": "• Subtítulos ASS incrustados (obliga a transcodificar)",
        "issue.bitrate": "• Bitrate de {mbps} Mbps, sobre el límite",
        "forgot": "Se borró la memoria de este chat.",
    },
}


def t(lang: str, key: str, **kw: object) -> str:
    table = STRINGS.get(lang) or STRINGS["en"]
    s = table.get(key) or STRINGS["en"].get(key) or key
    try:
        return s.format(**kw)
    except (KeyError, IndexError):
        return s


def render_details(lang: str, details: list[str]) -> str:
    """Render language-neutral detail codes produced by the flows."""
    lines: list[str] = []
    for d in details:
        parts = d.split(":", 3)
        code = parts[0]
        if code == "media":
            lines.append(t(lang, "detail.media", n=parts[1], size=human_bytes(int(parts[2]))))
        elif code == "torrent_delete":
            seeding = t(lang, "detail.seeding") if parts[2] == "1" else ""
            lines.append(
                t(lang, "detail.torrent_delete", size=human_bytes(int(parts[1])), seeding=seeding)
            )
        elif code == "torrent_kept":
            lines.append(t(lang, "detail.torrent_kept", size=human_bytes(int(parts[1]))))
        elif code in ("no_torrent", "no_hardlink_check"):
            lines.append(t(lang, f"detail.{code}"))
        elif code == "item" and len(parts) == 4:
            from smarrtbot.util import h

            lines.append(
                t(
                    lang,
                    "detail.item",
                    cat=parts[1],
                    size=human_bytes(int(parts[2])),
                    label=h(parts[3]),
                )
            )
    return "\n".join(lines)

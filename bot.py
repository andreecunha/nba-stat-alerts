"""Bot do Telegram: alertas de estatísticas de jogadores da NBA ao vivo.

Correr:  TELEGRAM_TOKEN=xxxx python bot.py
"""
import asyncio
import html
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from telegram.error import BadRequest
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import io
import math

import analise
import analise_img
from core import (norm, STATS, Sub, Tracker, alvo, box_espn, calendario_espn, confronto, cores, descr_linha, encontrar,
                  falta_dd, jogadores_espn, jogo_espn, linha_stats, marcador, nome_completo, parse_add, parse_seguir,
                  proximo_jogo, quando, resumo_dd, situacao_linha, texto_pedidos, valor_de, _numero)

def _ler_token() -> str:
    """Token da variável TELEGRAM_TOKEN ou, se não existir, do ficheiro token.txt ao lado do bot."""
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not token:
        caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.txt")
        try:
            with open(caminho, encoding="utf-8") as f:
                token = f.read().strip()
        except FileNotFoundError:
            pass
    if not token:
        raise SystemExit("Falta o token: define TELEGRAM_TOKEN ou cria um ficheiro token.txt ao lado do bot.py")
    return token


VERSAO = "2026-10-25 19:50 (linha só +1/-1)"
TOKEN = _ler_token()
INTERVALO = int(os.getenv("INTERVALO", "20"))  # segundos entre leituras
PASTA_BOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(PASTA_BOT, "subs.db"))
REMOVER_APOS = int(os.getenv("REMOVER_APOS", "10"))  # minutos depois do fim do jogo até sair da lista

# Dados ao vivo da ESPN (o cdn.nba.com bloqueia pedidos a partir de alguns países/redes).
ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ESPN_EQUIPAS = f"{ESPN}/teams"
ESPN_GAMELOG = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{}/gamelog?season={}"
NOVA_IORQUE = ZoneInfo("America/New_York")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json",
}

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # não escrever o token nem cada pedido no terminal
log = logging.getLogger("nba-alertas")


# ---------- dados da NBA / ESPN, com cache ----------
class Dados:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http
        self.cache: dict[str, tuple[float, object]] = {}

    async def _json(self, url: str, validade: float):
        t, valor = self.cache.get(url, (0, None))
        if valor is not None and time.time() - t < validade:
            return valor
        r = await self.http.get(url)
        r.raise_for_status()
        valor = r.json()
        self.cache[url] = (time.time(), valor)
        return valor

    async def _dia(self, data, validade: float) -> list[dict]:
        """Jogos de um dia (data dos EUA), já no formato da NBA."""
        j = await self._json(f"{ESPN}/scoreboard?dates={data:%Y%m%d}", validade)
        return [jogo_espn(ev) for ev in j.get("events", [])]

    async def jogos_hoje(self) -> list[dict]:
        """Jogos do dia nos EUA, mais os de ontem que ainda estejam a decorrer (jogos depois da meia-noite)."""
        hoje = datetime.now(NOVA_IORQUE).date()
        jogos = await self._dia(hoje, 10)
        try:
            jogos += [g for g in await self._dia(hoje - timedelta(days=1), 10) if g["gameStatus"] == 2]
        except Exception as e:
            log.warning("jogos de ontem: %s", e)
        return jogos

    async def boxscore(self, gid: str) -> dict:
        return box_espn(await self._json(f"{ESPN}/summary?event={gid}", 10), gid)

    async def calendario(self) -> list[dict]:
        """Jogos de ontem até daqui a 7 dias (atualizado de 3 em 3 h)."""
        t, valor = self.cache.get("calendario", (0, None))
        if valor is None or time.time() - t > 3 * 3600:
            hoje = datetime.now(NOVA_IORQUE).date()
            dias = await asyncio.gather(*(self._dia(hoje + timedelta(days=d), 3 * 3600) for d in range(-1, 8)),
                                        return_exceptions=True)
            valor = calendario_espn([g for d in dias if isinstance(d, list) for g in d])
            self.cache["calendario"] = (time.time(), valor)
        return valor

    async def jogadores(self) -> list[dict]:
        """Todos os jogadores da liga com a equipa (plantéis da ESPN, atualizados de 3 em 3 h por causa das lesões)."""
        t, valor = self.cache.get("jogadores", (0, None))
        if valor is not None and time.time() - t < 3 * 3600:
            return valor
        equipas = (await self._json(ESPN_EQUIPAS, 24 * 3600))["sports"][0]["leagues"][0]["teams"]

        async def plantel(e):
            r = await self.http.get(f"{ESPN_EQUIPAS}/{e['team']['id']}/roster")
            r.raise_for_status()
            return jogadores_espn(e["team"]["abbreviation"], r.json())

        listas = await asyncio.gather(*(plantel(e) for e in equipas))
        valor = [p for lista in listas for p in lista]
        self.cache["jogadores"] = (time.time(), valor)
        return valor

    async def gamelog(self, athlete_id: str, epoca: int) -> list[dict]:
        """Jogos de uma época de um jogador (já convertidos). Épocas passadas ficam 12 h em cache."""
        validade = 3600 if epoca >= analise.epoca_espn(datetime.now(timezone.utc)) else 12 * 3600
        try:
            return analise.parse_gamelog(await self._json(ESPN_GAMELOG.format(athlete_id, epoca), validade))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:   # jogador sem jogos nessa época
                return []
            raise


# ---------- base de dados ----------
def preparar_db():
    """Garante que a pasta da base de dados existe. Se não der (ex.: disco /data do Fly não criado),
    usa a pasta do bot e avisa nos logs em vez de o bot morrer ao arrancar."""
    global DB_PATH
    try:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        sqlite3.connect(DB_PATH).close()
    except (OSError, sqlite3.Error) as e:
        alternativa = os.path.join(PASTA_BOT, "subs.db")
        log.warning("Não consegui usar %s (%s). A usar %s — as listas perdem-se ao atualizar o bot.",
                    DB_PATH, e, alternativa)
        DB_PATH = alternativa
    log.info("Base de dados: %s", DB_PATH)


def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS subs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, jogador TEXT,
        stat TEXT, linha REAL, perto INTEGER)""")
    for coluna in ("fim REAL",        # quando o jogo acabou (para remoção automática)
                   "lado TEXT"):      # "over" ou "under"
        try:
            con.execute(f"ALTER TABLE subs ADD COLUMN {coluna}")
        except sqlite3.OperationalError:
            pass  # a coluna já existe
    return con


def fins(chat_id: int) -> dict[int, float]:
    with db() as con:
        return dict(con.execute("SELECT id, fim FROM subs WHERE chat_id = ? AND fim IS NOT NULL", (chat_id,)))


def todas_subs() -> list[Sub]:
    with db() as con:
        return [Sub(*r[:6], r[6] or "over")
                for r in con.execute("SELECT id, chat_id, jogador, stat, linha, perto, lado FROM subs")]


def subs_de(chat_id: int) -> list[Sub]:
    return [s for s in todas_subs() if s.chat_id == chat_id]


def descrever(s: Sub) -> str:
    t = f"<b>{html.escape(s.jogador)}</b> · {STATS[s.stat]['nome']}"
    if s.linha is not None:
        t += f" · {descr_linha(s)}"
    return t


# ---------- estado atual de um jogador ----------
async def procurar_ao_vivo(dados: Dados, nome: str):
    """Procura o jogador nos jogos a decorrer. Devolve (jogo, jogador) ou (None, None)."""
    for g in await dados.jogos_hoje():
        if g["gameStatus"] != 2:
            continue
        try:
            game = (await dados.boxscore(g["gameId"]))["game"]
        except Exception as e:
            log.warning("boxscore %s: %s", g["gameId"], e)
            continue
        achados = encontrar(game["homeTeam"].get("players", []) + game["awayTeam"].get("players", []), nome)
        if len(achados) == 1:
            return game, achados[0]
    return None, None


def texto_ao_vivo(s: Sub, game: dict, p: dict, tracker: Tracker | None) -> str:
    if p.get("status") == "INACTIVE":
        return f"🔴 Não está a jogar este jogo\n{marcador(game)}"
    st = p.get("statistics", {})
    valor = valor_de(s.stat, st)
    if tracker and s.id:
        tracker.semear(s, game["gameId"], st)
    alvo_dd = STATS[s.stat].get("dd")
    if alvo_dd:
        falta = falta_dd(st, alvo_dd)
        fim = "✅ já tem" if falta is None else f"{valor}/{alvo_dd} categorias · falta{'m' if falta > 1 else ''} {falta}"
        return f"🟢 {resumo_dd(st)} · {fim}\n{marcador(game)}"
    texto = f"🟢 <b>{valor}</b> {STATS[s.stat]['nome']}"
    if s.linha is not None:
        texto += f" · {situacao_linha(s, valor)}"
    return f"{texto}\n{marcador(game)}"


async def estado(dados: Dados, s: Sub, tracker: Tracker | None = None) -> str:
    """Linha de estado: o que tem agora, ou quanto falta para o jogo começar.
    Primeiro procura nos jogos a decorrer (só precisa dos jogos do dia); só depois usa plantéis e calendário."""
    agora = datetime.now(timezone.utc)
    try:
        game, p = await procurar_ao_vivo(dados, s.jogador)
        if p:
            return texto_ao_vivo(s, game, p, tracker)
    except Exception:
        log.exception("estado: jogos a decorrer")
    try:
        achados = encontrar(await dados.jogadores(), s.jogador)
        if len(achados) != 1:
            return ""
        equipa = achados[0]["team"]
        estados = {}
        try:
            estados = {g["gameId"]: g["gameStatus"] for g in await dados.jogos_hoje()}
        except Exception:
            pass
        jogo = proximo_jogo(await dados.calendario(), equipa, agora, estados)
        if jogo is None:
            return "Sem jogos marcados para já."
        if jogo["estado"] == 1:
            return f"⏳ {confronto(jogo['fora'], jogo['casa'])}\n{quando(jogo['inicio'], agora)}"
        if jogo["estado"] == 2:
            return f"🟢 O jogo {jogo['fora']} @ {jogo['casa']} está a decorrer, mas ainda não aparece na ficha do jogo."
    except Exception:
        log.exception("estado: plantéis/calendário")
    return ""


async def cmd_diag(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Testa cada fonte de dados e diz o que funciona."""
    dados: Dados = ctx.application.bot_data["dados"]
    dados.cache.clear()
    import analise_img as _img
    letras = "Inter ✅" if os.path.exists(os.path.join(_img.PASTA_FONTES, "Inter-Bold.otf")) else "❌ falta a pasta fontes"
    sem_acentos = "✅" if hasattr(_img, "limpo") else "❌ analise_img.py antigo"
    linhas = ["<b>Diagnóstico</b>", f"Versão: {VERSAO}", f"Letras do gráfico: {letras}",
              f"Gráfico sem acentos: {sem_acentos}", ""]
    vivos = []
    try:
        jogos = await dados.jogos_hoje()
        vivos = [g for g in jogos if g["gameStatus"] == 2]
        linhas.append(f"✅ ESPN jogos de hoje: {len(jogos)} ({len(vivos)} a decorrer)")
    except Exception as e:
        linhas.append(f"❌ ESPN jogos de hoje: {html.escape(repr(e))[:200]}")
    if vivos:
        try:
            game = (await dados.boxscore(vivos[0]["gameId"]))["game"]
            n = len(game["homeTeam"].get("players", [])) + len(game["awayTeam"].get("players", []))
            linhas.append(f"✅ ESPN ficha do jogo {vivos[0]['awayTeam']['teamTricode']}@{vivos[0]['homeTeam']['teamTricode']}: {n} jogadores")
        except Exception as e:
            linhas.append(f"❌ ESPN ficha do jogo: {html.escape(repr(e))[:200]}")
    try:
        linhas.append(f"✅ ESPN calendário: {len(await dados.calendario())} jogos")
    except Exception as e:
        linhas.append(f"❌ ESPN calendário: {html.escape(repr(e))[:200]}")
    todos = []
    try:
        todos = await dados.jogadores()
        lesionados = sum(1 for p in todos if p.get("lesao"))
        linhas.append(f"✅ ESPN plantéis: {len(todos)} jogadores ({lesionados} lesionados/em dúvida)")
    except Exception as e:
        linhas.append(f"❌ ESPN plantéis: {html.escape(repr(e))[:200]}")
    teste = next((p for p in todos if p.get("id") and p["name"] == "LeBron James"), None) or \
        next((p for p in todos if p.get("id")), None)
    if teste:
        try:
            ep = analise.epoca_espn(datetime.now(timezone.utc))
            n_atual = len(await dados.gamelog(teste["id"], ep))
            n_ant = len(await dados.gamelog(teste["id"], ep - 1))
            linhas.append(f"✅ ESPN histórico ({teste['name']}): {n_atual} jogos esta época, {n_ant} na anterior")
        except Exception as e:
            linhas.append(f"❌ ESPN histórico: {html.escape(repr(e))[:200]}")
    await update.message.reply_html("\n".join(linhas))


async def responder(q, texto: str | None = None, show_alert: bool = False):
    """Responde ao clique num botão. Se o clique for antigo (ex.: bot esteve parado), o Telegram recusa —
    ignoramos e continuamos, em vez de o bot rebentar."""
    try:
        await q.answer(texto, show_alert=show_alert)
    except BadRequest:
        pass


async def erro_geral(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    """Regista erros sem encher os logs com tracebacks de coisas sem importância."""
    e = ctx.error
    if isinstance(e, BadRequest) and ("too old" in str(e) or "not modified" in str(e)):
        return
    log.error("Erro ao tratar uma mensagem: %r", e, exc_info=e)


# ---------- menu principal (botões fixos em baixo) ----------
BOTAO_JOGOS, BOTAO_PROCURAR = "🏀 Jogos de hoje", "🔎 Procurar jogador"
BOTAO_LISTA, BOTAO_LIMPAR, BOTAO_AJUDA = "📋 A minha lista", "🧹 Limpar conversa", "❓ Ajuda"
MENU = ReplyKeyboardMarkup([[BOTAO_JOGOS, BOTAO_PROCURAR], [BOTAO_LISTA, BOTAO_LIMPAR], [BOTAO_AJUDA]],
                           resize_keyboard=True, is_persistent=True)
FECHAR = InlineKeyboardButton("✖️ Fechar", callback_data="fechar")

AJUDA = (
    "🏀 <b>NBA Alerts</b>\n\n"
    "Tudo funciona com os botões em baixo:\n\n"
    f"<b>{BOTAO_JOGOS}</b>\n"
    "Escolhe o jogo, a equipa e os jogadores (antes do jogo vês os plantéis, durante o jogo as stats).\n\n"
    f"<b>{BOTAO_PROCURAR}</b>\n"
    "Escreves o nome (ex.: lebron) e escolhes o jogador.\n\n"
    "Depois, para cada jogador: estatística → over/under → linha (com botões +/−). "
    "Na confirmação tens 📊 Ver análise com os últimos jogos.\n\n"
    f"<b>{BOTAO_LISTA}</b>\n"
    "Como está cada aposta, e botão para remover.\n\n"
    f"<b>{BOTAO_LIMPAR}</b>\n"
    "Apaga as mensagens da conversa. A tua lista fica guardada.\n\n"
    "Recebes aviso a cada cesto, quando a linha está perto, quando bate (ou o under se perde), "
    "estatísticas anuladas, 4–5 faltas e o resultado no fim.\n\n"
    "Os dados vêm da ESPN com cerca de 10–30 s de atraso em relação à TV."
)
TECLADO_AJUDA = InlineKeyboardMarkup([[InlineKeyboardButton("📖 Todas as estatísticas", callback_data="menu:pedidos")]])


TECLADO_INICIO = InlineKeyboardMarkup([
    [InlineKeyboardButton("🏀 Ver jogos de hoje", callback_data="menu:jogos"),
     InlineKeyboardButton("🔎 Procurar jogador", callback_data="menu:procurar")],
    [InlineKeyboardButton("Agora não", callback_data="fechar")]])
PERGUNTA_INICIO = "O que queres fazer? Escolher pelo jogo ou procurar um jogador pelo nome?"


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "Olá! 👋 Eu aviso-te no telemóvel quando os teus jogadores da NBA marcam, ressaltam, assistem…\n\n"
        "Usa os botões aqui em baixo 👇 — não precisas de escrever comandos.", reply_markup=MENU)
    await update.message.reply_html(PERGUNTA_INICIO, reply_markup=TECLADO_INICIO)


async def cmd_ajuda(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(AJUDA, reply_markup=TECLADO_AJUDA)


async def cmd_pedidos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(texto_pedidos())


async def fechar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await responder(q)
    ctx.application.bot_data.get("pendente", {}).pop(q.message.chat.id, None)
    try:
        await q.message.delete()
    except Exception:
        await q.edit_message_text("Menu fechado.")


async def menu_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await responder(q)
    if q.data == "menu:pedidos":
        await ctx.bot.send_message(q.message.chat.id, texto_pedidos(), parse_mode="HTML")
        return
    if q.data == "menu:procurar":
        try:
            await q.message.delete()
        except Exception:
            pass
        await pedir_nome(ctx, q.message.chat.id)
        return
    if q.data == "menu:jogos":
        try:
            await q.message.delete()
        except Exception:
            pass
        await enviar_jogos(ctx, q.message.chat.id)


# ---------- jogos de hoje ----------
async def enviar_jogos(ctx, chat_id: int, intro: str = ""):
    try:
        jogos = await ctx.application.bot_data["dados"].jogos_hoje()
    except Exception as e:
        log.warning("scoreboard: %s", e)
        await ctx.bot.send_message(chat_id, "Não consegui ler os jogos agora. Tenta daqui a pouco.")
        return
    if not jogos:
        await ctx.bot.send_message(chat_id, "Não há jogos da NBA hoje.")
        return
    ctx.application.bot_data.setdefault("jogos_listados", {})[chat_id] = [g["gameId"] for g in jogos]
    agora = datetime.now(timezone.utc)
    blocos = [intro] if intro else []
    for i, g in enumerate(jogos, 1):
        if g["gameStatus"] == 1 and g.get("gameTimeUTC"):
            inicio = datetime.fromisoformat(g["gameTimeUTC"].replace("Z", "+00:00"))
            blocos.append(f"{i}. {confronto(g['awayTeam']['teamTricode'], g['homeTeam']['teamTricode'])}\n{quando(inicio, agora)}")
        else:
            blocos.append(f"{i}. {marcador(g)}")
    blocos.append("📊 Que jogo queres ver?")
    botoes = [InlineKeyboardButton(str(i), callback_data=f"jogo:{i}") for i in range(1, len(jogos) + 1)]
    teclado = [botoes[i:i + 4] for i in range(0, len(botoes), 4)] + [[FECHAR]]
    await ctx.bot.send_message(chat_id, "\n\n".join(blocos), reply_markup=InlineKeyboardMarkup(teclado))


async def cmd_jogos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await enviar_jogos(ctx, update.effective_chat.id)


async def jogo_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await responder(q)
    await mostrar_jogo(ctx, q.message.chat.id, int(q.data.split(":")[1]))


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        n = int(ctx.args[0])
    except (IndexError, ValueError):
        n = None
    if n is None or not ctx.application.bot_data.get("jogos_listados", {}).get(chat_id):
        await enviar_jogos(ctx, chat_id, "👇 Primeiro escolhe o jogo. Estes são os jogos de hoje:")
        return
    await mostrar_jogo(ctx, chat_id, n)


async def mostrar_jogo(ctx, chat_id: int, n: int):
    """Stats de todos os jogadores do jogo N, com botões para escolher a equipa e seguir jogadores."""
    dados: Dados = ctx.application.bot_data["dados"]
    ids = ctx.application.bot_data.get("jogos_listados", {}).get(chat_id) or []
    if not 1 <= n <= len(ids):
        await enviar_jogos(ctx, chat_id, f"O jogo {n} não está na lista. Estes são os jogos de hoje:")
        return
    try:
        game = (await dados.boxscore(ids[n - 1]))["game"]
    except Exception as e:
        log.warning("stats: %s", e)
        await ctx.bot.send_message(chat_id, "Não consegui ler as stats desse jogo agora. Tenta daqui a pouco.")
        return
    if game.get("gameStatus") == 1:
        await mostrar_planteis(ctx, chat_id, game, ids[n - 1])
        return

    numerados, linhas, equipas = [], [marcador(game)], {}
    for equipa in (game["awayTeam"], game["homeTeam"]):
        tri = equipa["teamTricode"]
        linhas.append(f"\n<b>{cores(tri)} {tri}</b>")
        fora, ativos = [], []
        for p in equipa.get("players", []):
            if p.get("status") == "INACTIVE":
                fora.append(html.escape(nome_completo(p)))
                continue
            numerados.append(nome_completo(p))
            ativos.append(p)
            linhas.append(f"{len(numerados)}. <b>{html.escape(nome_completo(p))}</b>\n    {linha_stats(p)}")
        if fora:
            linhas.append(f"🔴 Não jogam: {', '.join(fora)}")
        # Para os botões: quem joga mais aparece primeiro.
        ativos.sort(key=lambda p: -int(p["statistics"].get("minutes", 0) or 0))
        equipas[tri] = [(nome_completo(p), int(p["statistics"].get("points", 0) or 0)) for p in ativos]
    ctx.application.bot_data.setdefault("stats_listados", {})[chat_id] = numerados
    ctx.application.bot_data.setdefault("escolha", {})[chat_id] = {"equipas": equipas, "sel": [], "msg": None}
    linhas.append("\n➕ Queres seguir algum jogador? Escolhe a equipa:")
    teclado = InlineKeyboardMarkup([[InlineKeyboardButton(f"{cores(t)} {t}", callback_data=f"eq:{t}") for t in equipas],
                                    [FECHAR]])
    texto = "\n".join(linhas)
    while texto:  # o Telegram aceita até 4096 caracteres por mensagem
        corte = texto.rfind("\n", 0, 4000) if len(texto) > 4000 else len(texto)
        ultimo = corte >= len(texto)
        await ctx.bot.send_message(chat_id, texto[:corte], parse_mode="HTML", reply_markup=teclado if ultimo else None)
        texto = texto[corte:].lstrip("\n")


POSICOES = {"G": "Base/Extremo", "PG": "Base", "SG": "Extremo", "F": "Extremo-poste", "SF": "Extremo",
            "PF": "Extremo-poste", "C": "Poste", "G-F": "Extremo", "F-C": "Poste", "F-G": "Extremo", "C-F": "Poste"}


async def mostrar_planteis(ctx, chat_id: int, game: dict, gid: str):
    """Jogo ainda por começar: mostra os plantéis (com lesões) e deixa escolher jogadores com botões."""
    dados: Dados = ctx.application.bot_data["dados"]
    fora_tri, casa_tri = game["awayTeam"]["teamTricode"], game["homeTeam"]["teamTricode"]
    try:
        todos = await dados.jogadores()
    except Exception as e:
        log.warning("plantéis: %s", e)
        await ctx.bot.send_message(chat_id, "⏳ Esse jogo ainda não começou e não consegui ler os plantéis agora.\n\n"
                                            "Podes seguir um jogador pelo nome, por exemplo: lebron")
        return
    inicio = ""
    try:
        jogo = next((j for j in await dados.calendario() if j["gameId"] == gid), None)
        if jogo:
            inicio = quando(jogo["inicio"], datetime.now(timezone.utc))
    except Exception:
        pass

    linhas = [f"⏳ {confronto(fora_tri, casa_tri)}" + (f"\n{inicio}" if inicio else ""), "",
              "Ainda não começou — estes são os plantéis:"]
    numerados, equipas = [], {}
    for tri in (fora_tri, casa_tri):
        plantel = sorted((p for p in todos if p["team"] == tri), key=lambda p: (bool(p.get("lesao")), p["name"]))
        linhas.append(f"\n<b>{cores(tri)} {tri}</b>")
        botoes = []
        for p in plantel:
            numerados.append(p["name"])
            extra = f" · 🚑 {html.escape(p['lesao'])}" if p.get("lesao") else ""
            pos = f" · {p['pos']}" if p.get("pos") else ""
            linhas.append(f"{len(numerados)}. <b>{html.escape(p['name'])}</b>{pos}{extra}")
            botoes.append((p["name"], ("🚑" if p.get("lesao") else p.get("pos") or "")))
        equipas[tri] = botoes
    if not numerados:
        await ctx.bot.send_message(chat_id, "⏳ Esse jogo ainda não começou e ainda não há plantéis disponíveis.\n\n"
                                            "Podes seguir um jogador pelo nome, por exemplo: lebron")
        return
    ctx.application.bot_data.setdefault("stats_listados", {})[chat_id] = numerados
    ctx.application.bot_data.setdefault("escolha", {})[chat_id] = {"equipas": equipas, "sel": [], "msg": None}
    linhas.append("\n🚑 = lesionado ou em dúvida (confirma antes de apostar)")
    linhas.append("\n➕ Queres seguir algum jogador? Escolhe a equipa:")
    teclado = InlineKeyboardMarkup([[InlineKeyboardButton(f"{cores(t)} {t}", callback_data=f"eq:{t}") for t in equipas],
                                    [FECHAR]])
    texto = "\n".join(linhas)
    while texto:
        corte = texto.rfind("\n", 0, 4000) if len(texto) > 4000 else len(texto)
        ultimo = corte >= len(texto)
        await ctx.bot.send_message(chat_id, texto[:corte], parse_mode="HTML", reply_markup=teclado if ultimo else None)
        texto = texto[corte:].lstrip("\n")


# ---------- escolher jogadores com botões ----------
def teclado_jogadores(esc: dict, tri: str) -> InlineKeyboardMarkup:
    botoes = []
    for i, (nome, rotulo) in enumerate(esc["equipas"][tri]):
        marca = "✅ " if nome in esc["sel"] else ""
        extra = f" · {rotulo}" if rotulo != "" else ""
        botoes.append(InlineKeyboardButton(f"{marca}{nome}{extra}", callback_data=f"pl:{tri}:{i}"))
    linhas = [botoes[i:i + 2] for i in range(0, len(botoes), 2)]
    outra = [t for t in esc["equipas"] if t != tri]
    fundo = [InlineKeyboardButton(f"🔁 {cores(outra[0])} {outra[0]}", callback_data=f"eq:{outra[0]}")] if outra else []
    fundo.append(InlineKeyboardButton(f"▶️ Seguir ({len(esc['sel'])})", callback_data="plok"))
    linhas += [fundo, [FECHAR]]
    return InlineKeyboardMarkup(linhas)


def texto_escolha(esc: dict, tri: str) -> str:
    texto = f"👆 Toca nos jogadores de <b>{cores(tri)} {tri}</b> que queres seguir."
    if esc["sel"]:
        texto += "\n\nEscolhidos: " + ", ".join(f"<b>{html.escape(n)}</b>" for n in esc["sel"])
    return texto + "\n\nNo fim carrega em ▶️ Seguir."


async def equipa_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await responder(q)
    chat_id = q.message.chat.id
    esc = ctx.application.bot_data.get("escolha", {}).get(chat_id)
    tri = q.data.split(":")[1]
    if not esc or tri not in esc["equipas"]:
        await q.message.reply_text("Este menu já expirou. Volta a escolher o jogo.", reply_markup=MENU)
        return
    if esc["msg"] == q.message.message_id:
        await q.edit_message_text(texto_escolha(esc, tri), parse_mode="HTML", reply_markup=teclado_jogadores(esc, tri))
    else:
        m = await ctx.bot.send_message(chat_id, texto_escolha(esc, tri), parse_mode="HTML",
                                       reply_markup=teclado_jogadores(esc, tri))
        esc["msg"] = m.message_id


async def jogador_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat.id
    esc = ctx.application.bot_data.get("escolha", {}).get(chat_id)
    _, tri, i = q.data.split(":")
    if not esc or tri not in esc["equipas"]:
        await responder(q, "Este menu já expirou.")
        return
    nome = esc["equipas"][tri][int(i)][0]
    if nome in esc["sel"]:
        esc["sel"].remove(nome)
        await responder(q, f"Tiraste {nome}")
    else:
        esc["sel"].append(nome)
        await responder(q, f"Escolheste {nome}")
    await q.edit_message_text(texto_escolha(esc, tri), parse_mode="HTML", reply_markup=teclado_jogadores(esc, tri))


async def jogadores_ok(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat.id
    esc = ctx.application.bot_data.get("escolha", {}).get(chat_id)
    if not esc or not esc["sel"]:
        await responder(q, "Toca primeiro num jogador para o escolheres.", show_alert=True)
        return
    await responder(q)
    nomes = list(esc["sel"])
    esc["sel"] = []
    await q.edit_message_text("Escolhidos: " + ", ".join(f"<b>{html.escape(n)}</b>" for n in nomes), parse_mode="HTML")
    await iniciar_fila(ctx, chat_id, nomes)


# ---------- perguntas: estatística → over/under → linha ----------
BOTOES_STATS = [("Pontos", "pts"), ("Ressaltos", "reb"), ("Assistências", "ast"),
                ("Triplos", "3pt"), ("Roubos", "stl"), ("Desarmes", "blk"),
                ("Pts+Res+Ast", "pra"), ("Pts+Res", "pr"), ("Pts+Ast", "pa"),
                ("Res+Ast", "ra"), ("Duplo-duplo", "dd"), ("Triplo-duplo", "td")]
CANCELAR = InlineKeyboardButton("✖️ Cancelar", callback_data="cancelar")


def teclado_stats() -> InlineKeyboardMarkup:
    botoes = [InlineKeyboardButton(t, callback_data=f"stat:{k}") for t, k in BOTOES_STATS]
    return InlineKeyboardMarkup([botoes[i:i + 3] for i in range(0, len(botoes), 3)] + [[CANCELAR]])


TECLADO_LADO = InlineKeyboardMarkup([
    [InlineKeyboardButton("📈 Over (mais de)", callback_data="lado:over"),
     InlineKeyboardButton("📉 Under (menos de)", callback_data="lado:under")],
    [InlineKeyboardButton("Sem linha (só avisos)", callback_data="lado:sem")],
    [CANCELAR]])


def pendente(ctx, chat_id: int) -> dict | None:
    return ctx.application.bot_data.setdefault("pendente", {}).get(chat_id)


def progresso(pend: dict) -> str:
    feito = pend["total"] - len(pend["fila"])
    return f" ({feito + 1} de {pend['total']})" if pend["total"] > 1 else ""


async def iniciar_fila(ctx, chat_id: int, nomes: list[str], stat=None, linha=None, lado="over", aviso: str = ""):
    """Começa as perguntas para uma lista de jogadores (um de cada vez)."""
    ctx.application.bot_data.setdefault("pendente", {})[chat_id] = {
        "fila": list(nomes), "total": len(nomes), "stat_fixa": stat, "stat": stat,
        "linha_fixa": linha, "lado_fixo": lado, "lado": None, "espera_linha": False}
    await proximo_passo(ctx, chat_id, aviso)


async def proximo_passo(ctx, chat_id: int, aviso: str = "", q=None):
    """Faz a pergunta seguinte para o jogador atual, ou adiciona-o se já houver tudo o que é preciso."""
    pend = pendente(ctx, chat_id)
    while pend and pend["fila"]:
        nome = html.escape(pend["fila"][0])
        if pend["stat"] is None:
            await ctx.bot.send_message(chat_id, f"{aviso}📊 Que estatística queres que eu notifique para "
                                                f"<b>{nome}</b>?{progresso(pend)}",
                                       parse_mode="HTML", reply_markup=teclado_stats())
            return
        if not STATS[pend["stat"]].get("dd") and pend["linha_fixa"] is None and pend["lado"] is None:
            await ctx.bot.send_message(chat_id, f"🎯 <b>{nome}</b> · {STATS[pend['stat']]['nome']}{progresso(pend)}\n\n"
                                                f"Apostaste no over ou no under?",
                                       parse_mode="HTML", reply_markup=TECLADO_LADO)
            return
        if pend["espera_linha"]:
            return
        # Já temos tudo: adiciona.
        linha = None if (STATS[pend["stat"]].get("dd") or pend["lado"] == "sem") else pend["linha_fixa"]
        lado = pend["lado"] if pend["lado"] in ("over", "under") else pend["lado_fixo"]
        await adicionar_atual(ctx, chat_id, linha, lado)
        pend = pendente(ctx, chat_id)


async def adicionar_atual(ctx, chat_id: int, linha, lado: str):
    pend = pendente(ctx, chat_id)
    nome = pend["fila"].pop(0)
    texto, sub = await criar_sub(ctx, chat_id, nome, pend["stat"], linha, lado)
    teclado = None
    if sub.linha is not None and not STATS[sub.stat].get("dd"):
        teclado = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Ver análise", callback_data=f"an:{sub.id}")]])
    await ctx.bot.send_message(chat_id, texto, parse_mode="HTML", reply_markup=teclado)
    # prepara o próximo jogador
    pend.update({"stat": pend["stat_fixa"], "lado": None, "espera_linha": False})
    if not pend["fila"]:
        ctx.application.bot_data["pendente"].pop(chat_id, None)


async def stat_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await responder(q)
    chat_id = q.message.chat.id
    pend = pendente(ctx, chat_id)
    if not pend or not pend["fila"]:
        await q.edit_message_text("Este pedido já expirou. Volta a escolher o jogador.")
        return
    pend["stat"] = q.data.split(":")[1]
    await q.edit_message_text(f"📊 <b>{html.escape(pend['fila'][0])}</b> · {STATS[pend['stat']]['nome']}",
                              parse_mode="HTML")
    await proximo_passo(ctx, chat_id)


async def lado_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await responder(q)
    chat_id = q.message.chat.id
    pend = pendente(ctx, chat_id)
    if not pend or not pend["fila"] or pend["stat"] is None:
        await q.edit_message_text("Este pedido já expirou. Volta a escolher o jogador.")
        return
    lado = q.data.split(":")[1]
    pend["lado"] = lado
    nome = html.escape(pend["fila"][0])
    if lado == "sem":
        await q.edit_message_text(f"🎯 <b>{nome}</b> · {STATS[pend['stat']]['nome']} · sem linha", parse_mode="HTML")
        await proximo_passo(ctx, chat_id)
        return
    pend["espera_linha"] = True
    media = await media_recente(ctx.application.bot_data["dados"], pend["fila"][0], pend["stat"])
    pend["media"] = media
    pend["linha_tmp"] = linha_inicial(pend["stat"], media)
    await q.edit_message_text(texto_seletor(pend), parse_mode="HTML", reply_markup=teclado_seletor(pend))


# Linha de partida quando não há histórico (valores típicos das casas de apostas).
LINHA_PADRAO = {"pts": 19.5, "reb": 6.5, "ast": 4.5, "3pt": 1.5, "stl": 0.5, "blk": 0.5,
                "pra": 29.5, "pr": 24.5, "pa": 24.5, "ra": 10.5}
GRANDES = ("pts", "pra", "pr", "pa", "ra")


def linha_inicial(stat: str, media) -> float:
    """Média recente arredondada para o x.5 mais próximo (ex.: 22.4 -> 22.5; 22.9 -> 22.5)."""
    if media is None:
        return LINHA_PADRAO.get(stat, 0.5)
    return max(0.5, math.floor(media) + 0.5)   # o x.5 mais próximo da média


async def media_recente(dados: Dados, nome: str, stat: str):
    """Média dos últimos 5 jogos (ou None se não der em poucos segundos)."""
    try:
        async def calcula():
            achados = [p for p in encontrar(await dados.jogadores(), nome) if p.get("id")]
            if len(achados) != 1:
                return None
            ep = analise.epoca_espn(datetime.now(timezone.utc))
            jogos = []
            for e in (ep, ep - 1):
                jogos += await dados.gamelog(achados[0]["id"], e)
                if len([g for g in jogos if g["stats"].get("minutes", 0) > 0]) >= 5:
                    break
            jogos.sort(key=lambda g: g["data"], reverse=True)
            vals = [valor_de(stat, g["stats"]) for g in jogos if g["stats"].get("minutes", 0) > 0][:5]
            return sum(vals) / len(vals) if vals else None
        return await asyncio.wait_for(calcula(), timeout=5)
    except Exception:
        return None


def texto_seletor(pend: dict) -> str:
    nome = html.escape(pend["fila"][0])
    lado = "📈 Over" if pend["lado"] == "over" else "📉 Under"
    texto = (f"🎯 <b>{nome}</b> · {STATS[pend['stat']]['nome']} · {lado}{progresso(pend)}\n\n"
             f"Linha: <b>{pend['linha_tmp']:g}</b>")
    if pend.get("media") is not None:
        texto += f"\n<i>média dos últimos 5 jogos: {pend['media']:.1f}</i>"
    return texto + "\n\nAjusta com os botões e confirma (ou escreve o número)."


def teclado_seletor(pend: dict) -> InlineKeyboardMarkup:
    # Só ±1: como começa sempre em x.5, a linha fica sempre em x.5 (23.5, 19.5…)
    linhas = [[InlineKeyboardButton("−1", callback_data="ln:-1"), InlineKeyboardButton("+1", callback_data="ln:+1")]]
    linhas.append([InlineKeyboardButton(f"✅ Confirmar {pend['linha_tmp']:g}", callback_data="ln:ok")])
    linhas.append([CANCELAR])
    return InlineKeyboardMarkup(linhas)


async def seletor_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat.id
    pend = pendente(ctx, chat_id)
    if not pend or not pend["fila"] or not pend.get("espera_linha"):
        await responder(q, "Este pedido já expirou.")
        return
    acao = q.data.split(":", 1)[1]
    if acao == "ok":
        await responder(q)
        linha = pend["linha_tmp"]
        pend["espera_linha"] = False
        lado = "Over" if pend["lado"] == "over" else "Under"
        await q.edit_message_text(f"🎯 <b>{html.escape(pend['fila'][0])}</b> · {STATS[pend['stat']]['nome']} · "
                                  f"{lado} {linha:g}", parse_mode="HTML")
        await adicionar_atual(ctx, chat_id, linha, pend["lado"])
        await proximo_passo(ctx, chat_id)
        return
    novo = max(0.5, round(pend["linha_tmp"] + float(acao), 1))
    if novo == pend["linha_tmp"]:
        await responder(q, "A linha mínima é 0.5")
        return
    await responder(q)
    pend["linha_tmp"] = novo
    await q.edit_message_text(texto_seletor(pend), parse_mode="HTML", reply_markup=teclado_seletor(pend))


async def cancelar_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await responder(q)
    ctx.application.bot_data.setdefault("espera_nome", set()).discard(q.message.chat.id)
    pend = ctx.application.bot_data.setdefault("pendente", {}).pop(q.message.chat.id, None)
    resto = len(pend["fila"]) if pend else 0
    extra = f" ({resto} jogador{'es' if resto != 1 else ''} por adicionar)" if resto else ""
    await q.edit_message_text(f"Cancelado{extra}. Nada mais foi adicionado.")


async def recebeu_linha(update: Update, ctx, pend: dict):
    """O utilizador escreveu o número da linha."""
    texto = update.message.text.strip().lower().replace(",", ".")
    linha = _numero(texto)
    if linha is None or linha < 0:
        await update.message.reply_text("Escreve só o número da linha, por exemplo 24.5 (ou carrega em Cancelar).")
        return
    pend["espera_linha"] = False
    await adicionar_atual(ctx, update.effective_chat.id, linha, pend["lado"])
    await proximo_passo(ctx, update.effective_chat.id)


# ---------- análise da aposta ----------
def sub_por_id(sub_id: int, chat_id: int) -> Sub | None:
    return next((s for s in subs_de(chat_id) if s.id == sub_id), None)


async def fazer_analise(dados: Dados, s: Sub):
    """Devolve (imagem PNG em bytes, legenda) ou levanta ValueError com uma mensagem para o utilizador."""
    achados = [p for p in encontrar(await dados.jogadores(), s.jogador) if p.get("id")]
    if len(achados) != 1:
        raise ValueError("Não encontrei o histórico deste jogador.")
    p = achados[0]
    agora = datetime.now(timezone.utc)

    # Adversário: o jogo a decorrer ou o próximo da equipa.
    estados = {}
    try:
        estados = {g["gameId"]: g["gameStatus"] for g in await dados.jogos_hoje()}
    except Exception:
        pass
    jogo = proximo_jogo(await dados.calendario(), p["team"], agora, estados)
    if not jogo:
        raise ValueError("Não encontrei o próximo jogo deste jogador.")
    adversario = jogo["casa"] if jogo["fora"] == p["team"] else jogo["fora"]
    quando_txt = "a decorrer" if jogo["estado"] == 2 else quando(jogo["inicio"], agora).split(" (")[-1].rstrip(")")

    # Histórico: época atual + anteriores (os jogos contra a mesma equipa são poucos por época).
    epoca = analise.epoca_espn(agora)
    resultados = await asyncio.gather(*(dados.gamelog(p["id"], e) for e in range(epoca, epoca - 4, -1)),
                                      return_exceptions=True)
    jogos = [g for r in resultados if isinstance(r, list) for g in r]
    if not jogos:
        erro = next((r for r in resultados if isinstance(r, Exception)), None)
        if erro:
            log.warning("gamelog %s: %r", p["name"], erro)
        raise ValueError("Não consegui obter o histórico de jogos deste jogador agora.")
    jogos.sort(key=lambda g: g["data"], reverse=True)
    ultimos, contra, min_rec, min_ep = analise.preparar(jogos, s.stat, adversario, epoca)

    img = await asyncio.to_thread(
        analise_img.gerar, p["name"], p["team"], adversario, STATS[s.stat]["nome"], s.linha, s.under,
        ultimos, contra, min_rec, min_ep, quando_txt, False, jogo["casa"] == p["team"])
    buf = io.BytesIO()
    await asyncio.to_thread(img.save, buf, "PNG")
    legenda = analise.resumo_texto(s.stat, s.linha, s.under, ultimos, contra, adversario)
    return buf.getvalue(), legenda


async def analise_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat.id
    s = sub_por_id(int(q.data.split(":")[1]), chat_id)
    if not s:
        await responder(q, "Já não segues esta aposta.", show_alert=True)
        return
    await responder(q, "A preparar a análise…")
    aguarda = await ctx.bot.send_message(chat_id, f"📊 A analisar {html.escape(s.jogador)}…", parse_mode="HTML")
    try:
        png, legenda = await fazer_analise(ctx.application.bot_data["dados"], s)
    except ValueError as e:
        await aguarda.edit_text(f"😕 {e}")
        return
    except Exception:
        log.exception("análise")
        await aguarda.edit_text("😕 Não consegui fazer a análise agora. Tenta daqui a pouco.")
        return
    try:
        await aguarda.delete()
    except Exception:
        pass
    await ctx.bot.send_photo(chat_id, InputFile(io.BytesIO(png), filename="analise.png"), caption=legenda)


# ---------- seguir pelo nome escrito ----------
async def resolver(dados: Dados, consulta: str):
    """Nome escrito -> ("ok", nome completo) | ("varios", [jogadores]) | ("nada", None)."""
    try:
        achados = encontrar(await dados.jogadores(), consulta)
    except Exception as e:
        log.warning("plantéis: %s", e)
        achados = None
    if achados and len(achados) > 1:
        return "varios", achados
    if achados:
        return "ok", nome_completo(achados[0])
    game, p = await procurar_ao_vivo(dados, consulta)
    if p:
        return "ok", nome_completo(p)
    if achados is None:
        return "ok", consulta  # sem plantéis nem jogo ao vivo: aceita o nome tal como está
    return "nada", None


async def seguir(update: Update, ctx: ContextTypes.DEFAULT_TYPE, args: list[str]):
    res, erro = parse_seguir(args)
    if erro:
        await update.message.reply_text(erro, reply_markup=MENU)
        return
    consulta, stat, linha, lado = res
    dados: Dados = ctx.application.bot_data["dados"]
    estado_nome, valor = await resolver(dados, consulta)
    aviso = ""
    if estado_nome == "nada" and len(consulta.split()) > 1:
        # Talvez a última palavra seja uma estatística que não conheço (ex.: "lebron rebotes").
        resto, palavra = consulta.rsplit(" ", 1)
        estado2, valor2 = await resolver(dados, resto)
        if estado2 != "nada":
            estado_nome, valor, stat = estado2, valor2, None
            aviso = f"Não conheço a estatística «{html.escape(palavra)}».\n\n"
    if estado_nome == "varios":
        opcoes = valor[:10]
        ctx.application.bot_data.setdefault("escolher_nome", {})[update.effective_chat.id] = {
            "nomes": [nome_completo(p) for p in opcoes], "stat": stat, "linha": linha, "lado": lado}
        botoes = [[InlineKeyboardButton(f"{nome_completo(p)} · {cores(p['team'])} {p['team']}", callback_data=f"nm:{i}")]
                  for i, p in enumerate(opcoes)]
        await update.message.reply_html("Há vários jogadores com esse nome. Qual deles?",
                                        reply_markup=InlineKeyboardMarkup(botoes + [[CANCELAR]]))
        return
    if estado_nome == "nada":
        await update.message.reply_html(f"Não encontrei nenhum jogador «{html.escape(consulta)}».\n\n"
                                        f"Verifica o nome ou carrega em <b>{BOTAO_JOGOS}</b> para escolheres da lista.",
                                        reply_markup=MENU)
        return
    await iniciar_fila(ctx, update.effective_chat.id, [valor], stat, linha, lado, aviso)


async def pedir_nome(ctx, chat_id: int):
    ctx.application.bot_data.setdefault("pendente", {}).pop(chat_id, None)
    ctx.application.bot_data.setdefault("espera_nome", set()).add(chat_id)
    await ctx.bot.send_message(chat_id, "✍️ Escreve o nome do jogador (ex.: <code>lebron</code>, <code>curry</code>, "
                                        "<code>jokic</code>)", parse_mode="HTML",
                               reply_markup=InlineKeyboardMarkup([[CANCELAR]]))


async def nome_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Escolheu um dos jogadores com o mesmo nome."""
    q = update.callback_query
    await responder(q)
    chat_id = q.message.chat.id
    esc = ctx.application.bot_data.get("escolher_nome", {}).pop(chat_id, None)
    i = int(q.data.split(":")[1])
    if not esc or i >= len(esc["nomes"]):
        await q.edit_message_text("Este pedido já expirou. Volta a procurar o jogador.")
        return
    await q.edit_message_text(f"👤 <b>{html.escape(esc['nomes'][i])}</b>", parse_mode="HTML")
    await iniciar_fila(ctx, chat_id, [esc["nomes"][i]], esc["stat"], esc["linha"], esc["lado"])


async def criar_sub(ctx, chat_id: int, nome: str, stat: str, linha, lado: str = "over") -> tuple[str, Sub]:
    """Guarda o seguimento e devolve a mensagem de confirmação com o estado atual."""
    lado = lado if linha is not None else "over"
    mesma_linha = lambda a, b: (a is None and b is None) or (a is not None and b is not None and abs(a - b) < 1e-9)
    acabados = fins(chat_id)
    parecidas = [x for x in subs_de(chat_id) if norm(x.jogador) == norm(nome) and x.stat == stat]
    igual = next((x for x in parecidas if mesma_linha(x.linha, linha) and (linha is None or x.lado == lado)), None)
    if igual and igual.id not in acabados:
        return f"ℹ️ {descrever(igual)} já está na tua lista.", igual
    with db() as con:
        if igual:   # a mesma aposta de um jogo que já acabou: troca pela nova (é para o próximo jogo)
            con.execute("DELETE FROM subs WHERE id = ?", (igual.id,))
        cur = con.execute("INSERT INTO subs (chat_id, jogador, stat, linha, perto, lado) VALUES (?,?,?,?,NULL,?)",
                          (chat_id, nome, stat, linha, lado))
        sub = Sub(cur.lastrowid, chat_id, nome, stat, linha, None, lado)
    outras = [x for x in parecidas if x is not igual and x.id not in acabados]
    linha_estado = await estado(ctx.application.bot_data["dados"], sub, ctx.application.bot_data["tracker"])
    texto = f"✅ A seguir: {descrever(sub)}"
    if outras:
        ja = ", ".join((descr_linha(x) if x.linha is not None else "sem linha") for x in outras)
        texto += f"\n<i>(já tinhas {STATS[stat]['nome']} deste jogador: {ja} — ficas com {'as duas' if len(outras) == 1 else 'todas'})</i>"
    if linha_estado:
        texto += f"\n\n{linha_estado}"
    return texto, sub


async def cmd_seguir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Escreve o nome do jogador, por exemplo: lebron", reply_markup=MENU)
        return
    await seguir(update, ctx, ctx.args)


async def texto_livre(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    chat_id = update.effective_chat.id
    if texto == BOTAO_JOGOS:
        await enviar_jogos(ctx, chat_id)
        return
    if texto == BOTAO_LISTA:
        await enviar_lista(ctx, chat_id)
        return
    if texto == BOTAO_AJUDA:
        await update.message.reply_html(AJUDA, reply_markup=TECLADO_AJUDA)
        return
    if texto == BOTAO_LIMPAR:
        await cmd_limpar(update, ctx)
        return
    if texto == BOTAO_PROCURAR:
        await pedir_nome(ctx, chat_id)
        return
    ctx.application.bot_data.setdefault("espera_nome", set()).discard(chat_id)
    pend = pendente(ctx, chat_id)
    if pend and pend.get("espera_linha"):
        await recebeu_linha(update, ctx, pend)
        return
    await seguir(update, ctx, texto.split())


# ---------- a minha lista ----------
async def texto_lista(ctx, chat_id: int) -> str | None:
    subs = subs_de(chat_id)
    if not subs:
        return None
    dados = ctx.application.bot_data["dados"]
    estados = await asyncio.gather(*(estado(dados, s) for s in subs))
    acabados = fins(chat_id)
    blocos = ["📋 <b>A tua lista</b>"]
    for i, (s, e) in enumerate(zip(subs, estados), 1):
        cabecalho = f"{i}. <b>{html.escape(s.jogador)}</b>"
        if s.linha is not None:
            cabecalho += f" · {descr_linha(s)}"
        if not e or not e.startswith("🟢"):
            cabecalho += f" · {STATS[s.stat]['nome']}"
        if s.id in acabados:
            falta = max(1, round((acabados[s.id] + REMOVER_APOS * 60 - time.time()) / 60))
            e = f"🏁 Jogo terminado · sai da lista daqui a {falta} min"
        blocos.append(cabecalho + (f"\n{e}" if e else ""))
    return "\n\n".join(blocos)


TECLADO_LISTA = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Atualizar", callback_data="lista:atualizar"),
                                       InlineKeyboardButton("🗑️ Remover", callback_data="rm:menu")]])


async def enviar_lista(ctx, chat_id: int):
    texto = await texto_lista(ctx, chat_id)
    if not texto:
        await ctx.bot.send_message(chat_id, f"Ainda não segues ninguém.\n\nCarrega em {BOTAO_JOGOS} para escolheres "
                                            f"jogadores, ou escreve um nome (ex.: lebron).", reply_markup=MENU)
        return
    await ctx.bot.send_message(chat_id, texto, parse_mode="HTML", reply_markup=TECLADO_LISTA)


async def cmd_lista(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await enviar_lista(ctx, update.effective_chat.id)


async def lista_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await responder(q, "A atualizar…")
    texto = await texto_lista(ctx, q.message.chat.id)
    try:
        await q.edit_message_text(texto or "A tua lista está vazia.", parse_mode="HTML",
                                  reply_markup=TECLADO_LISTA if texto else None)
    except Exception:
        pass  # nada mudou desde a última vez


def teclado_remover(chat_id: int) -> InlineKeyboardMarkup | None:
    subs = subs_de(chat_id)
    if not subs:
        return None
    linhas = [[InlineKeyboardButton(f"❌ {s.jogador} · {STATS[s.stat]['nome']}"
                                    + (f" · {'u' if s.under else 'o'}{s.linha:g}" if s.linha is not None else ""),
                                    callback_data=f"rm:{s.id}")] for s in subs]
    linhas.append([InlineKeyboardButton("🗑️ Remover todos", callback_data="rm:tudo"),
                   InlineKeyboardButton("✅ Feito", callback_data="fechar")])
    return InlineKeyboardMarkup(linhas)


async def remover_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat.id
    acao = q.data.split(":")[1]
    if acao == "menu":
        await responder(q)
        teclado = teclado_remover(chat_id)
        if teclado:
            await ctx.bot.send_message(chat_id, "🗑️ Toca em quem queres deixar de seguir:", reply_markup=teclado)
        return
    with db() as con:
        if acao == "tudo":
            con.execute("DELETE FROM subs WHERE chat_id = ?", (chat_id,))
        else:
            con.execute("DELETE FROM subs WHERE id = ? AND chat_id = ?", (int(acao), chat_id))
    await responder(q, "Removido")
    teclado = teclado_remover(chat_id)
    if teclado:
        await q.edit_message_reply_markup(teclado)
    else:
        await q.edit_message_text("A tua lista está vazia.")


async def cmd_remover(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subs = subs_de(update.effective_chat.id)
    if not ctx.args:
        teclado = teclado_remover(update.effective_chat.id)
        if teclado:
            await update.message.reply_text("🗑️ Toca em quem queres deixar de seguir:", reply_markup=teclado)
        else:
            await update.message.reply_text("A tua lista está vazia.", reply_markup=MENU)
        return
    if ctx.args[0].lower() == "tudo":
        ids = [s.id for s in subs]
    else:
        # Aceita "/r 1 2 5", "/r 1,2,5" e intervalos como "/r 2-4".
        numeros = set()
        try:
            for parte in " ".join(ctx.args).replace(",", " ").split():
                if "-" in parte:
                    a, b = (int(x) for x in parte.split("-", 1))
                    numeros.update(range(min(a, b), max(a, b) + 1))
                else:
                    numeros.add(int(parte))
        except ValueError:
            numeros = set()
        invalidos = sorted(n for n in numeros if not 1 <= n <= len(subs))
        if not numeros or invalidos:
            extra = f" Não existe: {', '.join(map(str, invalidos))}." if invalidos else ""
            await update.message.reply_text(f"Usa /r 1 2 5, /r 2-4 ou /r tudo (vê os números na lista).{extra}")
            return
        ids = [subs[n - 1].id for n in sorted(numeros)]
    removidos = [s for s in subs if s.id in ids]
    with db() as con:
        con.executemany("DELETE FROM subs WHERE id = ?", [(i,) for i in ids])
    if not removidos:
        await update.message.reply_text("Não tinhas nada para remover.")
        return
    lista = "\n".join(f"• {descrever(s)}" for s in removidos)
    await update.message.reply_html(f"🗑️ Removido{'s' if len(removidos) != 1 else ''}:\n\n{lista}")


# ---------- /add (atalho por números do /statsjogo) ----------
async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    nomes = ctx.application.bot_data.get("stats_listados", {}).get(update.effective_chat.id)
    if not nomes:
        await update.message.reply_text(f"Carrega em {BOTAO_JOGOS}, escolhe o jogo e depois a equipa — "
                                        f"aí escolhes os jogadores com botões.", reply_markup=MENU)
        return
    res, erro = parse_add(ctx.args)
    if erro:
        await update.message.reply_text(f"Não percebi. Coloca /add e o número do jogador.\n\n{erro}")
        return
    numeros, stat, linha = res
    invalidos = [n for n in numeros if not 1 <= n <= len(nomes)]
    if invalidos:
        await update.message.reply_text(f"Não existe: {', '.join(map(str, invalidos))}. Os números vão de 1 a {len(nomes)}.")
        return
    await iniciar_fila(ctx, update.effective_chat.id, [nomes[n - 1] for n in numeros], stat, linha)


# ---------- limpar ----------
async def cmd_limpar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Apaga as mensagens recentes da conversa (limite do Telegram: menos de 48 h).
    A lista de jogadores seguidos mantém-se."""
    chat_id = update.effective_chat.id
    ultimo = update.message.message_id
    falhas = 0
    for fim in range(ultimo, max(ultimo - 3000, 0), -100):
        ids = list(range(fim, max(fim - 100, 0), -1))
        try:
            await ctx.bot.delete_messages(chat_id, ids)
            falhas = 0
        except Exception:
            falhas += 1  # mensagens com mais de 48 h ou já apagadas
            if falhas >= 3:
                break
    n = len(subs_de(chat_id))
    lista = f" A tua lista ({n} aposta{'s' if n != 1 else ''}) continua guardada." if n else ""
    await ctx.bot.send_message(chat_id, f"🧹 Conversa limpa.{lista}", reply_markup=MENU)
    await ctx.bot.send_message(chat_id, PERGUNTA_INICIO, reply_markup=TECLADO_INICIO)


# ---------- ciclo de leitura ----------
async def ciclo(app: Application):
    dados: Dados = app.bot_data["dados"]
    tracker: Tracker = app.bot_data["tracker"]
    terminados: set[str] = set()
    while True:
        try:
            subs = todas_subs()
            if subs:
                for g in await dados.jogos_hoje():
                    gid = g["gameId"]
                    if g["gameStatus"] == 1 or gid in terminados:
                        continue
                    box = await dados.boxscore(gid)
                    for chat_id, texto in tracker.processar(box, subs):
                        try:
                            await app.bot.send_message(chat_id, texto, parse_mode="HTML")
                        except Exception as e:
                            log.warning("envio para %s falhou: %s", chat_id, e)
                    if box["game"]["gameStatus"] == 3:
                        terminados.add(gid)
                # Jogos acabados: marcar a hora do fim e remover da lista passados REMOVER_APOS minutos.
                agora = time.time()
                with db() as con:
                    if tracker.terminados:
                        con.executemany("UPDATE subs SET fim = ? WHERE id = ? AND fim IS NULL",
                                        [(agora, i) for i in tracker.terminados])
                        tracker.terminados.clear()
                    apagados = con.execute("DELETE FROM subs WHERE fim IS NOT NULL AND fim < ?",
                                           (agora - REMOVER_APOS * 60,)).rowcount
                if apagados:
                    log.info("Removidos automaticamente: %s", apagados)
        except Exception as e:
            log.warning("ciclo: %s", e)
        await asyncio.sleep(INTERVALO)


async def saude(reader, writer):
    """Responde 'ok' a quem bater na porta (o Fly.io verifica se a app está viva)."""
    try:
        await reader.read(1024)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
    finally:
        writer.close()


async def ao_iniciar(app: Application):
    # No Fly.io (ou se PORT estiver definido) abre uma porta só para responder "ok" à verificação de saúde.
    if os.getenv("PORT") or os.getenv("FLY_APP_NAME"):
        porta = int(os.getenv("PORT", "8080"))
        app.bot_data["saude"] = await asyncio.start_server(saude, "0.0.0.0", porta)
        log.info("Verificação de saúde na porta %s", porta)
    app.bot_data["dados"] = Dados(httpx.AsyncClient(headers=HEADERS, timeout=15))
    app.bot_data["tracker"] = Tracker()
    await app.bot.delete_my_commands()   # tudo funciona com botões; os comandos continuam a existir, escondidos
    app.bot_data["ciclo"] = asyncio.get_running_loop().create_task(ciclo(app))


def main():
    preparar_db()
    db().close()
    app = Application.builder().token(TOKEN).post_init(ao_iniciar).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["a", "ajuda", "help"], cmd_ajuda))
    app.add_handler(CommandHandler(["s", "seguir", "f"], cmd_seguir))
    app.add_handler(CommandHandler(["l", "lista"], cmd_lista))
    app.add_handler(CommandHandler(["p", "pedidos"], cmd_pedidos))
    app.add_handler(CommandHandler(["diag"], cmd_diag))
    app.add_handler(CommandHandler(["limpar", "clear", "c"], cmd_limpar))
    app.add_handler(CommandHandler(["r", "remover"], cmd_remover))
    app.add_handler(CommandHandler(["j", "jogos"], cmd_jogos))
    app.add_handler(CommandHandler(["statsjogo", "stats", "st"], cmd_stats))
    app.add_handler(CommandHandler(["add"], cmd_add))
    app.add_handler(CallbackQueryHandler(fechar, pattern=r"^fechar$"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(jogo_callback, pattern=r"^jogo:"))
    app.add_handler(CallbackQueryHandler(equipa_callback, pattern=r"^eq:"))
    app.add_handler(CallbackQueryHandler(jogador_callback, pattern=r"^pl:"))
    app.add_handler(CallbackQueryHandler(jogadores_ok, pattern=r"^plok$"))
    app.add_handler(CallbackQueryHandler(stat_callback, pattern=r"^stat:"))
    app.add_handler(CallbackQueryHandler(lado_callback, pattern=r"^lado:"))
    app.add_handler(CallbackQueryHandler(cancelar_callback, pattern=r"^cancelar$"))
    app.add_handler(CallbackQueryHandler(lista_callback, pattern=r"^lista:"))
    app.add_handler(CallbackQueryHandler(remover_callback, pattern=r"^rm:"))
    app.add_handler(CallbackQueryHandler(analise_callback, pattern=r"^an:"))
    app.add_handler(CallbackQueryHandler(seletor_callback, pattern=r"^ln:"))
    app.add_handler(CallbackQueryHandler(nome_callback, pattern=r"^nm:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, texto_livre))
    app.add_error_handler(erro_geral)
    log.info("Bot a correr (versão %s). Leitura a cada %ss.", VERSAO, INTERVALO)
    app.run_polling()


if __name__ == "__main__":
    main()

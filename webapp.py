"""Mini App do Telegram: servidor web (aiohttp) com a página da app e uma API JSON.

Corre no mesmo processo do bot, na porta PORT (8080 no Fly). Também responde "ok" em / para a
verificação de saúde do Fly. Cada pedido à API traz o initData do Telegram, que é validado com o
token do bot — assim sabemos quem é o utilizador sem login.
"""
import hashlib
import hmac
import json
import os
import re
import time
from statistics import mean, median
from urllib.parse import parse_qsl

from aiohttp import web

PASTA_APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
FOTO = "https://a.espncdn.com/i/headshots/nba/players/full/{}.png"


# ---------- autenticação (initData do Telegram) ----------
def validar_init_data(init_data: str, token: str, validade: int = 24 * 3600) -> dict | None:
    """Confirma que o initData foi assinado pelo Telegram para este bot. Devolve o utilizador ou None."""
    try:
        campos = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    recebido = campos.pop("hash", "")
    verificar = "\n".join(f"{k}={v}" for k, v in sorted(campos.items()))
    segredo = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calculado = hmac.new(segredo, verificar.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculado, recebido):
        return None
    if time.time() - int(campos.get("auth_date", "0") or 0) > validade:
        return None
    try:
        return json.loads(campos.get("user", "{}")) or None
    except ValueError:
        return None


def _sem_html(t: str) -> str:
    return re.sub(r"<[^>]+>", "", t or "").replace("&#x27;", "'").replace("&amp;", "&")


# ---------- aplicação web ----------
def criar_app(tg_app, api: dict) -> web.Application:
    """tg_app: Application do python-telegram-bot (para bot_data); api: funções do bot.py."""
    token = api["token"]
    STATS = api["STATS"]

    @web.middleware
    async def autenticar(request, handler):
        if request.path.startswith("/api/"):
            user = validar_init_data(request.headers.get("X-Init-Data", ""), token)
            if not user:
                return web.json_response({"erro": "Abre a app a partir do Telegram."}, status=401)
            request["chat_id"] = int(user["id"])
        return await handler(request)

    def dados():
        return tg_app.bot_data["dados"]

    async def logos():
        try:
            return await dados().logos()
        except Exception:
            return {}

    def equipa(t: dict, lg: dict) -> dict:
        tri = t["teamTricode"]
        return {"tri": tri, "score": t.get("score", 0), "logo": lg.get(tri, "")}

    # --- páginas ---
    async def saude(request):
        return web.Response(text="ok")

    async def pagina(request):
        return web.FileResponse(os.path.join(PASTA_APP, "index.html"),
                                headers={"Cache-Control": "no-cache"})

    # --- API ---
    async def config(request):
        return web.json_response({
            "stats": [{"id": k, "nome": v["nome"], "en": v["en"], "dd": bool(v.get("dd"))} for k, v in STATS.items()],
        })

    async def jogos(request):
        lg = await logos()
        try:
            lista = await dados().jogos_hoje()
        except Exception:
            return web.json_response({"erro": "Não consegui ler os jogos agora."}, status=502)
        agora = api["agora"]()
        out = []
        for g in lista:
            inicio = g.get("gameTimeUTC")
            out.append({
                "id": g["gameId"], "estado": g["gameStatus"],
                "fora": equipa(g["awayTeam"], lg), "casa": equipa(g["homeTeam"], lg),
                "relogio": api["relogio"](g) if g["gameStatus"] != 1 else "",
                "quando": api["quando"](api["data"](inicio), agora) if inicio and g["gameStatus"] == 1 else "",
            })
        return web.json_response(out)

    async def jogo(request):
        gid = request.match_info["gid"]
        lg = await logos()
        try:
            game = (await dados().boxscore(gid))["game"]
        except Exception:
            return web.json_response({"erro": "Não consegui ler este jogo agora."}, status=502)
        equipas = []
        if game.get("gameStatus") == 1:
            todos = await dados().jogadores()
            for lado in ("awayTeam", "homeTeam"):
                tri = game[lado]["teamTricode"]
                plantel = sorted((p for p in todos if p["team"] == tri), key=lambda p: (bool(p.get("lesao")), p["name"]))
                equipas.append({**equipa(game[lado], lg), "jogadores": [
                    {"nome": p["name"], "foto": FOTO.format(p["id"]) if p.get("id") else "", "pos": p.get("pos", ""),
                     "lesao": p.get("lesao", ""), "stats": None, "inativo": False} for p in plantel]})
        else:
            for lado in ("awayTeam", "homeTeam"):
                t = game[lado]
                jogadores = []
                for p in sorted(t.get("players", []), key=lambda p: (p.get("status") == "INACTIVE",
                                                                     -int(p["statistics"].get("minutes", 0) or 0))):
                    st = p["statistics"]
                    jogadores.append({
                        "nome": api["nome_completo"](p), "foto": FOTO.format(p["id"]) if p.get("id") else "",
                        "pos": "", "lesao": "", "inativo": p.get("status") == "INACTIVE",
                        "stats": {"pts": st.get("points", 0), "reb": st.get("reboundsTotal", 0),
                                  "ast": st.get("assists", 0), "3pt": st.get("threePointersMade", 0),
                                  "stl": st.get("steals", 0), "blk": st.get("blocks", 0),
                                  "min": st.get("minutes", 0), "pf": st.get("fouls", 0)}})
                equipas.append({**equipa(t, lg), "jogadores": jogadores})
        return web.json_response({"id": gid, "estado": game.get("gameStatus"),
                                  "relogio": api["relogio"](game) if game.get("gameStatus") != 1 else "",
                                  "equipas": equipas})

    async def procurar(request):
        q = (request.query.get("q") or "").strip()
        if len(q) < 2:
            return web.json_response([])
        lg = await logos()
        try:
            achados = api["encontrar"](await dados().jogadores(), q)[:15]
        except Exception:
            return web.json_response({"erro": "Não consegui procurar agora."}, status=502)
        return web.json_response([{"nome": p["name"], "team": p["team"], "logo": lg.get(p["team"], ""),
                                   "foto": FOTO.format(p["id"]) if p.get("id") else "", "pos": p.get("pos", ""),
                                   "lesao": p.get("lesao", "")} for p in achados])

    async def lista(request):
        chat_id = request["chat_id"]
        subs = api["subs_de"](chat_id)
        estados = []
        for s in subs:
            try:
                estados.append(_sem_html(await api["estado"](dados(), s)))
            except Exception:
                estados.append("")
        acabados = api["fins"](chat_id)
        out = []
        for s, e in zip(subs, estados):
            if s.id in acabados:
                e = "🏁 Jogo terminado"
            out.append({"id": s.id, "jogador": s.jogador, "stat": s.stat, "stat_nome": STATS[s.stat]["nome"],
                        "linha": s.linha, "lado": s.lado, "estado": e})
        return web.json_response(out)

    async def seguir(request):
        chat_id = request["chat_id"]
        try:
            corpo = await request.json()
            nome, stat = str(corpo["jogador"]), str(corpo["stat"])
            linha = corpo.get("linha")
            linha = float(linha) if linha is not None else None
            lado = corpo.get("lado") or "over"
        except (ValueError, KeyError, TypeError):
            return web.json_response({"erro": "Pedido inválido."}, status=400)
        if stat not in STATS or lado not in ("over", "under") or (linha is not None and linha < 0):
            return web.json_response({"erro": "Pedido inválido."}, status=400)
        if STATS[stat].get("dd"):
            linha = None
        ctx = api["ctx"]()
        texto, sub = await api["criar_sub"](ctx, chat_id, nome, stat, linha, lado)
        repetida = texto.startswith("ℹ️")
        # Confirmação também no chat (com o botão de análise), como quando se segue pelos botões.
        try:
            await api["enviar_confirmacao"](chat_id, texto, sub)
        except Exception:
            pass
        return web.json_response({"ok": True, "repetida": repetida, "msg": _sem_html(texto).split("\n")[0],
                                  "id": sub.id})

    async def remover(request):
        chat_id = request["chat_id"]
        sid = int(request.match_info["sid"])
        api["remover"](chat_id, sid)
        return web.json_response({"ok": True})

    async def media(request):
        nome, stat = request.query.get("jogador", ""), request.query.get("stat", "")
        if stat not in STATS or STATS[stat].get("dd"):
            return web.json_response({"media": None, "linha": None})
        m = await api["media_recente"](dados(), nome, stat)
        return web.json_response({"media": m, "linha": api["linha_inicial"](stat, m)})

    async def analise(request):
        nome, stat = request.query.get("jogador", ""), request.query.get("stat", "")
        if stat not in STATS or STATS[stat].get("dd"):
            return web.json_response({"erro": "Sem análise para esta estatística."}, status=400)
        try:
            a = await api["dados_analise"](dados(), nome, stat)
        except ValueError as e:
            return web.json_response({"erro": str(e)}, status=404)
        except Exception:
            return web.json_response({"erro": "Não consegui fazer a análise agora."}, status=502)
        lg = await logos()
        vals = [v for _, _, v in a["ultimos"]]
        return web.json_response({
            "jogador": a["jogador"]["name"], "equipa": a["jogador"]["team"], "adversario": a["adversario"],
            "logo_adv": lg.get(a["adversario"], ""), "em_casa": a["em_casa"], "quando": a["quando"],
            "ultimos": [{"rot": r, "data": d, "v": v} for r, d, v in a["ultimos"]],
            "contra": [{"rot": r, "data": d, "v": v} for r, d, v in a["contra"]],
            "media": round(mean(vals), 1) if vals else None, "mediana": median(vals) if vals else None,
            "min": min(vals) if vals else None, "max": max(vals) if vals else None,
            "min_rec": round(a["min_rec"]), "min_ep": round(a["min_ep"]),
        })

    app = web.Application(middlewares=[autenticar])
    app.add_routes([
        web.get("/", saude), web.get("/app", pagina), web.get("/app/", pagina),
        web.static("/app/fontes", os.path.join(PASTA_APP, "fontes")),
        web.get("/api/config", config), web.get("/api/jogos", jogos), web.get("/api/jogo/{gid}", jogo),
        web.get("/api/procurar", procurar), web.get("/api/lista", lista), web.post("/api/seguir", seguir),
        web.delete("/api/lista/{sid}", remover), web.get("/api/media", media), web.get("/api/analise", analise),
    ])
    return app

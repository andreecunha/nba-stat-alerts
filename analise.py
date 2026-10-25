"""Análise de uma aposta com base no histórico jogo a jogo do jogador (gamelog da ESPN).

Sem rede: recebe o JSON da ESPN e devolve os dados prontos para o gráfico (analise_img.gerar).
"""
from datetime import datetime, timezone
from statistics import mean

from core import ESPN_PARA_NBA, STATS, _int, valor_de

# Rótulos do gamelog da ESPN -> campos usados no resto do código
LABELS_GAMELOG = {"PTS": "points", "REB": "reboundsTotal", "AST": "assists", "STL": "steals",
                  "BLK": "blocks", "PF": "fouls", "MIN": "minutes"}


def epoca_espn(agora: datetime) -> int:
    """A ESPN identifica a época pelo ano em que acaba: 2026-27 -> 2027."""
    return agora.year + 1 if agora.month >= 8 else agora.year


def rotulo_epoca(data: datetime) -> str:
    """'24/25' para um jogo da época 2024-25."""
    inicio = data.year if data.month >= 8 else data.year - 1
    return f"{inicio % 100:02d}/{(inicio + 1) % 100:02d}"


def _data(t: str) -> datetime:
    t = (t or "").replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        d = datetime.strptime(t[:10], "%Y-%m-%d")
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_gamelog(j: dict) -> list[dict]:
    """Gamelog da ESPN -> [{'id', 'data', 'adv', 'casa', 'stats'}], do mais recente para o mais antigo.
    Ignora a pré-época (só época regular, play-in e playoffs)."""
    labels = j.get("labels") or j.get("names") or []
    eventos = j.get("events") or {}
    jogos = {}
    for tipo in j.get("seasonTypes", []):
        nome_tipo = (tipo.get("displayName") or "").lower()
        if "preseason" in nome_tipo or "pre season" in nome_tipo:
            continue
        for cat in tipo.get("categories", []):
            if cat.get("type") not in (None, "event"):
                continue
            for ev in cat.get("events", []):
                eid = str(ev.get("eventId") or ev.get("id") or "")
                meta = eventos.get(eid, {})
                if not eid or not meta or eid in jogos:
                    continue
                stats = {}
                for label, valor in zip(labels, ev.get("stats", [])):
                    if label in LABELS_GAMELOG:
                        stats[LABELS_GAMELOG[label]] = _int(valor)
                    elif label == "3PT":
                        stats["threePointersMade"] = _int(valor)   # "3-7" -> 3
                adv = (meta.get("opponent") or {}).get("abbreviation", "?")
                jogos[eid] = {"id": eid, "data": _data(meta.get("gameDate", "")),
                              "adv": ESPN_PARA_NBA.get(adv, adv),
                              "casa": (meta.get("atVs") or "vs").strip() != "@", "stats": stats}
    return sorted(jogos.values(), key=lambda g: g["data"], reverse=True)


def preparar(jogos: list[dict], stat: str, adversario: str, epoca_atual: int, n: int = 5):
    """Dados para o gráfico a partir de jogos de várias épocas (mais recente primeiro).
    Devolve (ultimos, contra, minutos_recentes, minutos_epoca); listas do mais antigo para o mais recente."""
    jogados = [g for g in jogos if g["stats"].get("minutes", 0) > 0]   # ignora jogos em que não entrou
    recentes = jogados[:n]
    vs = [g for g in jogados if g["adv"] == adversario][:n]

    def barra(g, rotulo):
        return (rotulo, g["data"].strftime("%d/%m"), valor_de(stat, g["stats"]))

    ultimos = [barra(g, f"vs {g['adv']}" if g["casa"] else f"@{g['adv']}") for g in reversed(recentes)]
    contra = [barra(g, rotulo_epoca(g["data"])) for g in reversed(vs)]
    min_rec = mean(g["stats"].get("minutes", 0) for g in recentes) if recentes else 0
    da_epoca = [g for g in jogados if (g["data"].year + (1 if g["data"].month >= 8 else 0)) == epoca_atual]
    min_ep = mean(g["stats"].get("minutes", 0) for g in da_epoca) if da_epoca else min_rec
    return ultimos, contra, min_rec, min_ep


def resumo_texto(stat: str, linha: float, under: bool, ultimos, contra, adversario: str) -> str:
    """Legenda curta para acompanhar a imagem."""
    nome = STATS[stat]["nome"]
    ok = (lambda v: v < linha) if under else (lambda v: v > linha)
    partes = []
    if ultimos:
        vals = [v for _, _, v in ultimos]
        partes.append(f"Últimos {len(vals)}: {sum(map(ok, vals))}/{len(vals)} · média {mean(vals):.1f} {nome}")
    if contra:
        vals = [v for _, _, v in contra]
        partes.append(f"Contra {adversario}: {sum(map(ok, vals))}/{len(vals)} · média {mean(vals):.1f}")
    return "\n".join(partes)

"""Gera a imagem da análise de uma aposta (últimos jogos + jogos contra o adversário).

Só usa Pillow (leve, cabe na máquina de 256 MB do Fly).
"""
import os
from statistics import mean, median

from PIL import Image, ImageDraw, ImageFont

PASTA_FONTES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fontes")
_SISTEMA = "/usr/share/fonts/opentype/inter"


def _fonte(nome: str, tam: int):
    for pasta in (PASTA_FONTES, _SISTEMA):
        caminho = os.path.join(pasta, nome)
        if os.path.exists(caminho):
            return ImageFont.truetype(caminho, tam)
    return ImageFont.load_default(tam)


# Paleta (igual à foto de perfil do bot)
FUNDO = (14, 20, 44)
PAINEL = (24, 32, 64)
GRELHA = (44, 54, 92)
BRANCO = (255, 255, 255)
CINZA = (150, 160, 190)
VERMELHO = (230, 30, 50)
VERDE = (38, 196, 112)
FALHA = (232, 72, 85)
AMARELO = (255, 196, 61)

W, H = 1080, 1500
M = 56  # margem lateral


def _texto(d, xy, txt, fonte, cor, ancora="la"):
    d.text(xy, txt, font=fonte, fill=cor, anchor=ancora)


def _linha_tracejada(d, x0, x1, y, cor, larg=4, traco=18, espaco=12):
    x = x0
    while x < x1:
        d.line([(x, y), (min(x + traco, x1), y)], fill=cor, width=larg)
        x += traco + espaco


def _painel(img, d, topo, titulo, jogos, linha, under, altura=470):
    """Um gráfico de barras: jogos = [(rótulo, sublinha, valor), ...] do mais antigo para o mais recente."""
    esq, dir_ = M, W - M
    d.rounded_rectangle([esq, topo, dir_, topo + altura], radius=28, fill=PAINEL)
    if not jogos:
        _texto(d, (esq + 32, topo + 30), titulo, _fonte("Inter-Bold.otf", 34), BRANCO)
        _texto(d, (W / 2, topo + altura / 2 + 20), "Sem jogos recentes para mostrar",
               _fonte("Inter-Medium.otf", 28), CINZA, "mm")
        return topo + altura

    acima = sum(1 for _, _, v in jogos if (v < linha if under else v > linha))
    f_tit, f_res = _fonte("Inter-Bold.otf", 34), _fonte("InterDisplay-Black.otf", 40)
    _texto(d, (esq + 32, topo + 30), titulo, f_tit, BRANCO)
    cor_res = VERDE if acima * 2 > len(jogos) else (AMARELO if acima * 2 == len(jogos) else FALHA)
    _texto(d, (dir_ - 32, topo + 26), f"{acima}/{len(jogos)}", f_res, cor_res, "ra")

    # Área do gráfico
    g_esq, g_dir = esq + 70, dir_ - 118
    g_top, g_base = topo + 110, topo + altura - 96
    vmax = max([v for _, _, v in jogos] + [linha]) * 1.18
    y_de = lambda v: g_base - (v / vmax) * (g_base - g_top)

    f_eixo = _fonte("Inter-Medium.otf", 22)
    passo = 10 if vmax > 30 else 5 if vmax > 12 else 2
    v = 0
    while v <= vmax:
        y = y_de(v)
        d.line([(g_esq, y), (g_dir, y)], fill=GRELHA, width=2)
        _texto(d, (g_esq - 14, y), str(v), f_eixo, CINZA, "rm")
        v += passo

    n = len(jogos)
    slot = (g_dir - g_esq) / n
    larg_barra = min(slot * 0.56, 120)
    f_val, f_rot, f_sub = _fonte("Inter-Black.otf", 32), _fonte("Inter-Bold.otf", 24), _fonte("Inter-Regular.otf", 20)
    for i, (rot, sub, val) in enumerate(jogos):
        cx = g_esq + slot * (i + 0.5)
        ok = val < linha if under else val > linha
        cor = VERDE if ok else FALHA
        d.rounded_rectangle([cx - larg_barra / 2, y_de(val), cx + larg_barra / 2, g_base],
                            radius=10, fill=cor)
        _texto(d, (cx, g_base + 16), rot, f_rot, BRANCO, "mt")
        _texto(d, (cx, g_base + 48), sub, f_sub, CINZA, "mt")

    # Linha da aposta (etiqueta na margem direita, fora das barras)
    yl = y_de(linha)
    _linha_tracejada(d, g_esq, g_dir + 8, yl, AMARELO)
    f_l = _fonte("Inter-Bold.otf", 22)
    etiqueta = f"{linha:g}"
    tw = d.textlength(etiqueta, font=f_l) + 20
    d.rounded_rectangle([g_dir + 14, yl - 18, g_dir + 14 + tw, yl + 18], radius=8, fill=AMARELO)
    _texto(d, (g_dir + 14 + tw / 2, yl), etiqueta, f_l, FUNDO, "mm")

    for i, (rot, sub, val) in enumerate(jogos):
        cx = g_esq + slot * (i + 0.5)
        d.text((cx, y_de(val) - 10), str(val), font=f_val, fill=BRANCO, anchor="ms",
               stroke_width=6, stroke_fill=PAINEL)
    return topo + altura


def gerar(jogador: str, equipa: str, adversario: str, stat_nome: str, linha: float, under: bool,
          ultimos: list, contra: list, minutos_recentes: float, minutos_epoca: float,
          quando: str = "", exemplo: bool = False, em_casa: bool = True) -> Image.Image:
    img = Image.new("RGB", (W, H), FUNDO)
    d = ImageDraw.Draw(img)

    # Cabeçalho
    _texto(d, (M, 54), jogador.upper(), _fonte("InterDisplay-Black.otf", 58), BRANCO)
    lado = "UNDER" if under else "OVER"
    tag = f"{lado} {linha:g} {stat_nome.upper()}"
    f_tag = _fonte("Inter-Black.otf", 30)
    b = d.textbbox((0, 0), tag, font=f_tag)
    d.rounded_rectangle([M, 138, M + b[2] - b[0] + 40, 138 + 52], radius=12, fill=VERMELHO)
    _texto(d, (M + 20, 164), tag, f_tag, BRANCO, "lm")
    sub = f"{equipa} {'vs' if em_casa else '@'} {adversario}" + (f" · {quando}" if quando else "")
    _texto(d, (M + b[2] - b[0] + 64, 164), sub, _fonte("Inter-SemiBold.otf", 28), CINZA, "lm")

    y = 222
    y = _painel(img, d, y, f"Últimos {len(ultimos)} jogos", ultimos, linha, under) + 28
    y = _painel(img, d, y, f"Últimos {len(contra)} vs {adversario}", contra, linha, under) + 28

    # Resumo em números
    vals = [v for _, _, v in ultimos] or [0]
    cartoes = [("MÉDIA", f"{mean(vals):.1f}"), ("MEDIANA", f"{median(vals):g}"),
               ("MÍN–MÁX", f"{min(vals)}–{max(vals)}"),
               ("MINUTOS", f"{minutos_recentes:.0f}" + (" ▲" if minutos_recentes > minutos_epoca + 1
                                                          else " ▼" if minutos_recentes < minutos_epoca - 1 else ""))]
    larg = (W - 2 * M - 3 * 18) / 4
    f_k, f_v = _fonte("Inter-Bold.otf", 20), _fonte("InterDisplay-Black.otf", 40)
    for i, (k, v) in enumerate(cartoes):
        x0 = M + i * (larg + 18)
        d.rounded_rectangle([x0, y, x0 + larg, y + 118], radius=20, fill=PAINEL)
        _texto(d, (x0 + larg / 2, y + 46), k, f_k, CINZA, "ms")
        _texto(d, (x0 + larg / 2, y + 96), v, f_v, BRANCO, "ms")
    y += 118 + 18
    nota = f"Últimos {len(ultimos)} jogos · minutos na época: {minutos_epoca:.0f}"
    _texto(d, (W / 2, y), nota, _fonte("Inter-Regular.otf", 22), CINZA, "mt")

    # Rodapé
    d.line([(M, H - 92), (W - M, H - 92)], fill=GRELHA, width=2)
    f_p, f_b = _fonte("Inter-Medium.otf", 24), _fonte("Inter-Black.otf", 24)
    a, b_ = "Powered by ", "André Cunha Analytics"
    wa = d.textlength(a, font=f_p)
    wb = d.textlength(b_, font=f_b)
    x = (W - wa - wb) / 2
    _texto(d, (x, H - 54), a, f_p, CINZA, "lm")
    _texto(d, (x + wa, H - 54), b_, f_b, VERMELHO, "lm")
    if exemplo:
        _texto(d, (W - M, H - 54), "dados de exemplo", _fonte("Inter-Regular.otf", 18), CINZA, "rm")
    return img


if __name__ == "__main__":
    # Exemplo com dados inventados, só para ver o aspeto.
    ultimos = [("@DEN", "15/10", 18), ("vs PHX", "17/10", 27), ("@SAC", "19/10", 21),
               ("vs MIN", "21/10", 31), ("@POR", "23/10", 24)]
    contra = [("23/24", "04/01", 26), ("23/24", "16/03", 19), ("24/25", "25/12", 29),
              ("24/25", "07/02", 33), ("25/26", "14/11", 22)]
    gerar("LeBron James", "LAL", "GSW", "pontos", 22.5, False, ultimos, contra,
          34.6, 33.1, "hoje às 03:30", exemplo=True).save("exemplo_analise.png")
    print("ok")

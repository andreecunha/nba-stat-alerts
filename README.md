# 🏀 Alertas NBA — bot do Telegram

Notificações no telemóvel, em tempo real, sempre que um jogador da NBA soma pontos, ressaltos, assistências, triplos, roubos ou desarmes. Avisa também quando completa um duplo-duplo ou um triplo-duplo.

As apps de resultados avisam quando a tua equipa marca, mas não quando *um jogador* chega a X pontos. Este bot faz isso: escreves `lebron 24.5` e recebes um aviso a cada cesto, outro quando estiver perto da linha, outro quando a bater e o resultado no fim do jogo.

*Real-time NBA player stat alerts on Telegram: points, rebounds, assists, threes, double-doubles and triple-doubles, with optional prop lines.*

---

## Exemplos de alertas

```
⚠️ PERTO DA LINHA — faltam 3
🏀 LeBron James +2 pontos → 22
Linha 24.5 · faltam 3
LAL 80–75 GSW · 3.º P 5:12
```

```
✅ LINHA BATIDA — mais de 24.5 pontos
🏀 LeBron James +3 pontos → 25
LAL 95–90 GSW · 4.º P 8:02
```

```
⚠️ PERTO DO TRIPLO-DUPLO — faltam 2
🎯 Nikola Jokić chegou aos 10 → 2/3 para o triplo-duplo
14 pts · 10 res · 8 ast
```

## Funcionalidades

- **Aviso a cada evento** da estatística que escolheres
- **Linhas de aposta over e under**: quanto falta (over) ou quanto ainda pode fazer (under), aviso quando está perto, quando bate ou se perde, ✅/❌ no fim
- **Duplo-duplo e triplo-duplo**: aviso quando cada categoria chega aos 10 e quando completa
- **Combinações**: pontos+ressaltos+assistências (PRA), PR, PA, RA
- **Estado ao seguir**: se o jogador já está em campo, mostra logo quanto tem; se não, quanto falta para o jogo começar (hora de Lisboa)
- **Avisos de início de jogo** e de **jogador fora do jogo** (lesão, por exemplo)
- **Lista sempre limpa**: cada jogador sai sozinho da lista 10 min depois do fim do jogo
- **Estatísticas anuladas**: aviso quando a NBA retira um cesto, ressalto, etc. (e se a linha deixar de estar batida)
- **Faltas**: aviso às 4 e 5 faltas e quando o jogador sai à 6.ª
- **📊 Análise da aposta**: botão na confirmação que gera um gráfico com os últimos 5 jogos e os últimos 5 contra o adversário, com a linha marcada, média, mediana, mín–máx e minutos
- **Plantéis antes do jogo**: nos jogos por começar mostra os plantéis com lesões (🚑) e deixa escolher jogadores com botões
- **Multiutilizador**: cada pessoa tem a sua lista; funciona também em grupos
- Sem barra: basta escrever `jokic ressaltos 11.5`

## 📱 Mini App

Além do chat, o bot tem uma app dentro do Telegram (botão **📱 Abrir app** ao lado da caixa de texto):
jogos de hoje com logos, plantéis com fotos e lesões, procura de jogadores, seletor da linha com régua e a
média dos últimos 5 jogos, análise com gráfico e a tua lista de apostas ao vivo (atualiza a cada 10 s, com barra
de progresso até à linha, ✅/❌ e destaque quando o valor sobe). O 🔔 no canto superior direito mostra as
notificações (cestos, linhas batidas, stats anuladas, faltas), as mesmas que chegam no chat.
A lista tem duas abas, «Os meus jogos» e «Ao vivo», agrupadas por jogo; as apostas já resolvidas descem para o fim.
Os jogos que estão a 15 minutos de começar têm uma luz vermelha a piscar.
No 🔔 cada notificação apaga-se a deslizar para a esquerda (ou todas de uma vez), e cada aposta desliza para a
esquerda para a silenciar (sem avisos, mas continua na lista) ou remover.

A app é servida pelo próprio bot (`webapp.py` + `app/index.html`) e precisa de HTTPS — no Fly.io fica em
`https://<nome-da-app>.fly.dev/app` e o bot configura o botão sozinho. Noutro alojamento, define `WEBAPP_URL`.

## Como usar

Tudo funciona com botões, sem comandos. O menu fica fixo em baixo:

**🏀 Jogos de hoje · 🔎 Procurar jogador · 📋 A minha lista · 🧹 Limpar conversa · ❓ Ajuda**

1. **🏀 Jogos de hoje** → tocas no jogo → escolhes a equipa → tocas nos jogadores (✅) → **▶️ Seguir**
   (antes do jogo aparecem os plantéis com lesões 🚑; durante o jogo, as stats)
2. Ou **🔎 Procurar jogador** → escreves o nome (ex.: `lebron`); se houver vários, escolhes num botão
3. Para cada jogador: **estatística** → **Over / Under / Sem linha** → **linha** com botões −5 −1 −0.5 +0.5 +1 +5,
   que começa já na média dos últimos 5 jogos (também podes escrever o número)
4. Na confirmação, **📊 Ver análise** mostra o gráfico dos últimos jogos

**📋 A minha lista** mostra como está cada aposta e tem botões para **🔄 Atualizar** e **🗑️ Remover**.
**🧹 Limpar conversa** apaga as mensagens mas mantém a lista.

Atalho para quem prefere escrever: `lebron pontos 24.5` (over) ou `lebron pontos under 24.5`.

## Criar o teu próprio bot

### 1. Token do Telegram
No Telegram, abre o **@BotFather**, envia `/newbot` e guarda o token. **Nunca o ponhas no código nem no GitHub.**

### 2a. Alojar no Railway (recomendado)
1. Faz *fork* deste repositório.
2. Em [railway.com](https://railway.com), escolhe **New Project → Deploy from GitHub repo** e seleciona o fork.
3. Em **Variables**, adiciona:
   - `TELEGRAM_TOKEN` = o teu token
   - `DB_PATH` = `/data/subs.db`
4. Adiciona um **volume** montado em `/data`, para as listas sobreviverem às atualizações.

O arranque já está configurado em `railway.json`.

### 2b. Correr no teu PC
Requer Python 3.10+.
```bash
pip install -r requirements.txt
TELEGRAM_TOKEN=o_teu_token python bot.py
```
No Windows (cmd): `set TELEGRAM_TOKEN=o_teu_token` e depois `py bot.py`.

### 2c. Servidor Ubuntu (VPS)
```bash
bash instalar.sh
```
Instala tudo e cria um serviço `systemd` que arranca sozinho.

### Variáveis opcionais
| Variável | Por defeito | O que faz |
|---|---|---|
| `INTERVALO` | `10` | Segundos entre leituras dos jogos |
| `DB_PATH` | `subs.db` | Onde guardar as listas |
| `REMOVER_APOS` | `10` | Minutos depois do fim do jogo até o jogador sair da lista |
| `WEBAPP_URL` | `https://<FLY_APP_NAME>.fly.dev/app` | Endereço da Mini App (tem de ser HTTPS) |

## Como funciona

- **Estatísticas ao vivo**: endpoints públicos da ESPN (scoreboard e summary), lidos a cada 10 s
- **Calendário**: scoreboard da ESPN dos próximos 7 dias
- **Equipa de cada jogador e lesões**: plantéis públicos da ESPN
- **Histórico para a análise**: gamelog público da ESPN (época atual e 3 anteriores)
- **Guardar as listas**: SQLite

```
webapp.py     servidor da Mini App (API + página), com validação do initData do Telegram
app/          página da Mini App e letra Barlow (licença SIL OFL, ver app/fontes/OFL.txt)
analise.py    histórico jogo a jogo (gamelog da ESPN) e dados da análise
analise_img.py  gráfico da análise (Pillow)
fontes/       letra Inter (licença SIL OFL, ver fontes/OFL.txt)
core.py       lógica (interpretar pedidos, alertas, duplo/triplo-duplo, calendário), sem rede
bot.py        Telegram, leitura dos dados e base de dados
test_core.py  simula jogos completos e verifica os alertas
```

Testes: `python test_core.py`

## Detalhes

- **"Mais de" é estritamente acima**: linha 24.5 precisa de 25, linha 10 precisa de 11.
- **Atraso** de cerca de 10–30 s em relação à TV.
- **Correções de estatística** da NBA são tratadas: se o valor descer abaixo da linha, os avisos voltam a ficar ativos.
- **Reiniciar a meio de um jogo** não reenvia alertas antigos.
- Os dados são públicos mas **não oficiais**: se a ESPN mudar o formato, pode ser preciso um ajuste.

## Aviso

Projeto pessoal, sem ligação à NBA, à ESPN nem a casas de apostas. Os dados podem ter atrasos ou erros: confirma sempre na fonte oficial. Aposta com responsabilidade.

## Licença

[MIT](LICENSE)

"""Bot do Telegram: avisa quando os sistemas tiverem problema e responde comandos.

Modos (python bot.py <modo>):
  checar   verifica se os sistemas estao no ar; avisa so quando o estado MUDA
  escutar  fica ouvindo comandos: /status /atrasados /os /estoque /frota /ajuda

So le dados (usuario claude_leitura). Responde apenas ao TELEGRAM_CHAT_ID.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg
from dotenv import load_dotenv

AQUI = Path(__file__).parent
load_dotenv(AQUI / ".env")

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DB_PROD = os.environ.get("DATABASE_URL_PROD", "")
DB_FROTA = os.environ.get("DATABASE_URL_FROTA", "")
ESTADO = AQUI / "estado.json"

# Monitor de erros (opcional): so liga se SENTRY_DSN estiver definido.
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(dsn=SENTRY_DSN, send_default_pii=False, traces_sample_rate=0)

# (nome, url, timeout). O Render gratis "dorme": a 1a resposta pode levar ~30 s.
SITES = [
    ("ProdManutencao", os.environ.get("URL_PROD", "https://prodmanutencao.onrender.com/health"), 60),
    ("GestFrota API", os.environ.get("URL_FROTA", "https://gestfrota-api.onrender.com/"), 90),
    ("GestFrota tela", os.environ.get("URL_FROTA_WEB", "https://frota-asje.onrender.com/"), 90),
]
ATIVOS = ["aberta", "em_execucao", "aguardando_peca"]


# ---------------------------------------------------------------- telegram
def _api(metodo, **dados):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{metodo}",
        data=urllib.parse.urlencode(dados).encode(),
    )
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.load(r)


def enviar(texto):
    _api("sendMessage", chat_id=CHAT_ID, text=texto[:4000])


# ---------------------------------------------------------------- banco (somente leitura)
def consultar(url, sql, params=None):
    with psycopg.connect(url, prepare_threshold=None, connect_timeout=15) as conn:
        try:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = 15000")
            return conn.execute(sql, params or {}).fetchall()
        finally:
            conn.rollback()


# ---------------------------------------------------------------- consultas
def atrasados():
    return consultar(DB_PROD, """
        select a.tag, coalesce(ca.categoria, '-'), p.proxima_prevista
        from plano_manutencao p
        join ativos a on a.id = p.ativo_id
        left join catalogo_atividades ca on ca.id = p.atividade_id
        where p.proxima_prevista < current_date
        order by p.proxima_prevista limit 15""")


def total_atrasados():
    return consultar(DB_PROD, "select count(*) from plano_manutencao where proxima_prevista < current_date")[0][0]


def os_abertas():
    return consultar(DB_PROD, """
        select o.numero_os, o.tipo, o.status, a.tag, coalesce(t.nome, 'SEM TECNICO'),
               (current_date - (o.aberta_em at time zone 'America/Sao_Paulo')::date) as dias
        from ordens_servico o
        join ativos a on a.id = o.ativo_id
        left join tecnicos t on t.id = o.responsavel_id
        where o.status = any(%(s)s) order by o.aberta_em limit 15""", {"s": ATIVOS})


def estoque_baixo():
    return consultar(DB_PROD, """
        select nome_peca, quantidade_atual, quantidade_minima from estoque_pecas
        where quantidade_atual <= quantidade_minima order by nome_peca limit 20""")


def abastecimentos_suspeitos():
    return consultar(DB_FROTA, """
        select v.placa, a.litros, a.km, a.created_at::date
        from abastecimentos a join veiculos v on v.id = a.veiculo_id
        where a.litros > 300 and a.created_at > now() - interval '30 days'
        order by a.created_at desc limit 10""")


# ---------------------------------------------------------------- comandos
def cmd_atrasados():
    r = atrasados()
    if not r:
        return "Nenhuma manutencao atrasada."
    return f"Atrasadas no plano (total {total_atrasados()}):\n" + "\n".join(
        f"- {t} | {c} | previsto {d}" for t, c, d in r)


def cmd_os():
    r = os_abertas()
    if not r:
        return "Nenhuma OS em aberto."
    return "OS em aberto:\n" + "\n".join(
        f"- {tp} ({st}) {tag} | {tec} | {d} dias" for _n, tp, st, tag, tec, d in r)


def cmd_estoque():
    r = estoque_baixo()
    if not r:
        return "Nenhuma peca abaixo do minimo."
    return "Estoque baixo:\n" + "\n".join(f"- {n}: {a} (minimo {m})" for n, a, m in r)


def cmd_frota():
    r = abastecimentos_suspeitos()
    if not r:
        return "Nenhum abastecimento suspeito nos ultimos 30 dias."
    return "Abastecimentos suspeitos (>300 L):\n" + "\n".join(
        f"- {p}: {float(l):.0f} L, km {k}, {d}" for p, l, k, d in r)


def checar_sites():
    """Devolve {nome: (ok, detalhe)}."""
    res = {}
    for nome, url, tmo in SITES:
        for tentativa in (1, 2):
            try:
                with urllib.request.urlopen(url, timeout=tmo) as r:
                    res[nome] = (200 <= r.status < 400, f"HTTP {r.status}")
            except Exception as e:  # noqa: BLE001
                res[nome] = (False, str(e)[:120])
            if res[nome][0]:
                break
            if tentativa == 1:
                time.sleep(20)  # tenta de novo antes de dar o alarme
    for nome, url in (("Banco ProdManutencao", DB_PROD), ("Banco GestFrota", DB_FROTA)):
        try:
            consultar(url, "select 1")
            res[nome] = (True, "ok")
        except Exception as e:  # noqa: BLE001
            res[nome] = (False, str(e)[:120])
    return res


def cmd_status():
    return "Status:\n" + "\n".join(
        f"{'OK  ' if ok else 'FALHA'} {n}: {d}" for n, (ok, d) in checar_sites().items())


COMANDOS = {
    "/status": cmd_status, "/atrasados": cmd_atrasados, "/os": cmd_os,
    "/estoque": cmd_estoque, "/frota": cmd_frota,
}
AJUDA = "Comandos: /status /atrasados /os /estoque /frota"


# ---------------------------------------------------------------- modos
def modo_checar():
    antes = json.loads(ESTADO.read_text()) if ESTADO.exists() else {}
    r = checar_sites()
    agora = {n: ok for n, (ok, _) in r.items()}
    for nome, ok in agora.items():
        # 1a falha ja avisa; falhas seguidas nao repetem; recuperacao avisa uma vez
        if not ok and antes.get(nome, True):
            enviar(f"PROBLEMA: {nome} fora do ar. Detalhe: {r[nome][1]}")
        elif ok and not antes.get(nome, True):
            enviar(f"Recuperado: {nome} voltou ao normal.")
    ESTADO.write_text(json.dumps(agora))


def modo_responder():
    """Uma passada so: responde comandos pendentes e sai (para rodar no GitHub Actions)."""
    ups = _api("getUpdates", timeout=0).get("result", [])
    if not ups:
        return
    # confirma ja de cara: se algo falhar depois, nao responde duas vezes
    _api("getUpdates", offset=ups[-1]["update_id"] + 1, timeout=0)
    for up in ups:
        msg = up.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
            continue  # ignora qualquer outra pessoa
        cmd = (msg.get("text") or "").split("@")[0].split()[:1]
        fn = COMANDOS.get(cmd[0].lower()) if cmd else None
        try:
            enviar(fn() if fn else AJUDA)
        except Exception as e:  # noqa: BLE001
            enviar(f"Erro ao consultar: {str(e)[:200]}")


def modo_escutar():
    offset = None
    enviar("Bot no ar. " + AJUDA)
    while True:
        try:
            dados = {"timeout": 50}
            if offset:
                dados["offset"] = offset
            for up in _api("getUpdates", **dados).get("result", []):
                offset = up["update_id"] + 1
                msg = up.get("message") or {}
                if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                    continue  # ignora qualquer outra pessoa
                cmd = (msg.get("text") or "").split("@")[0].split()[:1]
                fn = COMANDOS.get(cmd[0].lower()) if cmd else None
                try:
                    enviar(fn() if fn else AJUDA)
                except Exception as e:  # noqa: BLE001
                    enviar(f"Erro ao consultar: {str(e)[:200]}")
        except Exception as e:  # noqa: BLE001
            print("erro de rede:", e, file=sys.stderr)
            time.sleep(10)


if __name__ == "__main__":
    if not (TOKEN and CHAT_ID):
        sys.exit("Faltam TELEGRAM_TOKEN e TELEGRAM_CHAT_ID no bot/.env")
    modo = sys.argv[1] if len(sys.argv) > 1 else "escutar"
    {"checar": modo_checar, "escutar": modo_escutar, "responder": modo_responder}[modo]()

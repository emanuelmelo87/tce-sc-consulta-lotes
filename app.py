from flask import Flask, render_template, request, jsonify, session, Response, stream_with_context
import requests
import queue
import uuid
import threading
import json

from esfinge_executor import (
    EsfingeExecutor, LICENSES_URL, ENCERRAMENTO_URL
)

app = Flask(__name__)
app.secret_key = "tce-sc-local-tool"

# ── Estado global de execuções (multi-usuário) ────────────────────────────────
_active = {}   # { execution_id: { executor, queue, status } }

def _run(eid):
    sess = _active.get(eid)
    if not sess:
        return
    try:
        sess["executor"].executar()
    finally:
        status = sess["executor"].relatorio.get("status", "unknown")
        sess["queue"].put(("done", status))
        sess["status"] = status
        # Limpa da memória após 30 min
        threading.Timer(1800, lambda: _active.pop(eid, None)).start()

URL_PROD   = "https://api.virtual.tce.sc.gov.br"
URL_TESTES = "https://virtual.testing.tce.sc.gov.br"


def base_url(senha):
    return URL_TESTES if senha == "123456" else URL_PROD


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/consulta-lotes")
@app.route("/consulta-lotes.html")
def consulta_lotes_page():
    return render_template("consulta-lotes.html")


@app.route("/cadastro-erros")
@app.route("/cadastro-erros.html")
def cadastro_erros_page():
    return render_template("cadastro-erros.html")


@app.route("/agrupador-erros")
@app.route("/agrupador-erros.html")
def agrupador_erros_page():
    return render_template("agrupador-erros.html")


@app.route("/executor")
@app.route("/executor.html")
def executor_page():
    return render_template("executor.html")


@app.route("/api/autenticar", methods=["POST"])
def autenticar():
    body = request.json
    codigo_ug     = body.get("codigoUg", "")
    codigo_acesso = body.get("codigoAcesso", "")
    senha         = body.get("senha", "")

    url = f"{base_url(senha)}/esfingeonline/autenticacao/login"
    try:
        resp = requests.post(
            url,
            headers={"codigoAcesso": codigo_acesso, "senha": senha},
            params={
                "codigoUg": codigo_ug,
                "descricaoEmpresaTI": "BETHA SISTEMAS LTDA",
                "descritivoSoftware": "PRESTAÇÃO DE CONTAS",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("chave")
        if not token:
            return jsonify({"erro": "Token não retornado", "detalhe": data}), 400
        # guarda na sessão do servidor para reuso
        session["token"] = token
        session["base_url"] = base_url(senha)
        return jsonify({"ok": True, "token_preview": token[:30] + "..."})
    except requests.HTTPError as e:
        return jsonify({"erro": f"HTTP {e.response.status_code}", "detalhe": e.response.text}), 400
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route("/api/consultar-lote", methods=["POST"])
def consultar_lote():
    body       = request.json
    modulo     = body.get("modulo", "tributario")   # "tributario" ou "notafiscal"
    num_lote   = body.get("numeroLote", "")
    token      = body.get("token") or session.get("token")
    url_base   = body.get("baseUrl") or session.get("base_url", URL_PROD)

    if not token:
        return jsonify({"erro": "Sem token — autentique primeiro."}), 401
    if not num_lote:
        return jsonify({"erro": "Número do lote não informado."}), 400

    path_map = {
        "tributario":  f"/esfingeonline/v5/tributario/consultarPorNumeroLote/{num_lote}",
        "notafiscal":  f"/esfingeonline/v5/notafiscal/consultarPorNumeroLote/{num_lote}",
    }
    path = path_map.get(modulo)
    if not path:
        return jsonify({"erro": "Módulo inválido"}), 400

    try:
        resp = requests.get(
            f"{url_base}{path}",
            headers={"AUTH_TOKEN": token, "Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.HTTPError as e:
        try:
            detalhe = e.response.json()
        except Exception:
            detalhe = e.response.text
        return jsonify({"erro": f"HTTP {e.response.status_code}", "detalhe": detalhe}), e.response.status_code
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


# ── Executor: rotas ──────────────────────────────────────────────────────────

@app.route("/api/executor/iniciar", methods=["POST"])
def executor_iniciar():
    body  = request.json or {}
    auth  = body.get("authorization", "").strip()
    ua    = body.get("userAccess", "").strip()
    comp  = body.get("competencia", "").strip()
    if not auth or not ua or not comp:
        return jsonify({"erro": "authorization, userAccess e competencia são obrigatórios"}), 400

    eid = str(uuid.uuid4())
    q   = queue.Queue()
    exe = EsfingeExecutor(
        authorization     = auth,
        user_access       = ua,
        competencia       = comp,
        etapa_inicial     = int(body.get("etapaInicial") or 1),
        assunto_inicial   = int(body.get("assuntoInicial") or 0),
        visibilidade_publica = bool(body.get("visibilidadePublica", True)),
        log_callback      = lambda m: q.put(("log", m)),
        status_callback   = lambda s: q.put(("status", s)),
    )
    _active[eid] = {"executor": exe, "queue": q, "status": "running"}
    threading.Thread(target=_run, args=(eid,), daemon=True).start()
    return jsonify({"execution_id": eid})


@app.route("/api/executor/stream/<eid>")
def executor_stream(eid):
    sess = _active.get(eid)
    if not sess:
        def nf():
            yield f'data: {json.dumps({"type":"error","msg":"Execução não encontrada"})}\n\n'
        return Response(nf(), mimetype="text/event-stream")

    def generate():
        q = sess["queue"]
        while True:
            try:
                tipo, msg = q.get(timeout=25)
                yield f"data: {json.dumps({'type': tipo, 'msg': msg})}\n\n"
                if tipo == "done":
                    return
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/executor/parar/<eid>", methods=["POST"])
def executor_parar(eid):
    sess = _active.get(eid)
    if sess:
        sess["executor"].stop()
        return jsonify({"ok": True})
    return jsonify({"erro": "Não encontrado"}), 404


@app.route("/api/executor/relatorio/<eid>")
def executor_relatorio(eid):
    sess = _active.get(eid)
    if not sess:
        return jsonify({"erro": "Não encontrado"}), 404
    return jsonify(sess["executor"].relatorio)


@app.route("/api/executor/consultar-entidade", methods=["POST"])
def executor_consultar_entidade():
    body = request.json or {}
    auth = body.get("authorization", "").strip()
    ua   = body.get("userAccess", "").strip()
    if not auth or not ua:
        return jsonify({"erro": "Campos obrigatórios"}), 400
    try:
        resp = requests.get(
            f"{LICENSES_URL}/entidades/atual/",
            headers={"Content-Type": "application/json",
                     "Authorization": f"bearer {auth}", "User-Access": ua},
            timeout=30,
        )
        resp.raise_for_status()
        d = resp.json()
        return jsonify({"nome": d.get("nome", "?"), "id": d.get("id", "?")})
    except requests.HTTPError as e:
        return jsonify({"erro": f"HTTP {e.response.status_code}"}), 400
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route("/api/executor/consultar-encerramento", methods=["POST"])
def executor_consultar_encerramento():
    body = request.json or {}
    auth = body.get("authorization", "").strip()
    ua   = body.get("userAccess", "").strip()
    ano  = body.get("ano", "").strip()
    if not auth or not ua or not ano:
        return jsonify({"erro": "Campos obrigatórios"}), 400
    try:
        resp = requests.get(
            f"{ENCERRAMENTO_URL}/{ano}",
            headers={"Authorization": f"bearer {auth}", "User-Access": ua},
            timeout=30,
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.HTTPError as e:
        return jsonify({"erro": f"HTTP {e.response.status_code}"}), 400
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


if __name__ == "__main__":
    print("Acesse: http://localhost:5000")
    app.run(debug=False, port=5000)

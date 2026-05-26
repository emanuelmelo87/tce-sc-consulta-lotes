from flask import Flask, render_template, request, jsonify, session
import requests

app = Flask(__name__)
app.secret_key = "tce-sc-local-tool"

URL_PROD   = "https://api.virtual.tce.sc.gov.br"
URL_TESTES = "https://virtual.testing.tce.sc.gov.br"


def base_url(senha):
    return URL_TESTES if senha == "123456" else URL_PROD


@app.route("/")
def index():
    return render_template("index.html")


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


if __name__ == "__main__":
    print("Acesse: http://localhost:5000")
    app.run(debug=False, port=5000)

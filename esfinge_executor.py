"""
Executor e-Sfinge Online 2026 — Tributos
Portado de executar_esfinge.py para uso como módulo Flask (sem Tkinter, sem disco).
Toda saída vai via callbacks de log/status; ZIPs são analisados em memória.
"""

import requests
import zipfile
import io
import re
import time
import json
from datetime import datetime

# ── URLs ──────────────────────────────────────────────────────────────────────
BASE_URL         = "https://tributos.suite.betha.cloud/extensoes/scripts"
STATUS_URL       = "https://plataforma-execucoes-v2.betha.cloud/api/consulta"
DOWNLOAD_URL     = "https://plataforma-execucoes.betha.cloud/v1/download/api/execucoes"
LICENSES_URL     = "https://plataforma-licencas.betha.cloud/licenses/v0.1/api"
ENCERRAMENTO_URL = "https://tributos.suite.betha.cloud/dados/v1/encerramento-mensal"

POLLING_INTERVAL   = 20   # segundos
MAX_NETWORK_RETRIES = 5

# ── Assuntos da Etapa 01 ──────────────────────────────────────────────────────
ASSUNTOS_ETAPA_01 = [
    ("saldosIniciaisCreditoTributario",             "[00] Saldo Inicial de Créditos Tributários"),
    ("cadastrosContribuintes",                       "[01] Cadastro Contribuinte"),
    ("cadastrosImobiliarios",                        "[02] Cadastro Imobiliário"),
    ("cadastrosPropriedadesImobiliarias",            "[03] Cadastro de Propriedade Imobiliária"),
    ("lancamentosCreditosTributarios",               "[04] Lançamento de Créditos Tributários"),
    ("cobrancaDividaAtiva",                          "[05] Cobrança de Dívida Ativa"),
    ("inicioPrazoPrescricional",                     "[06] Início do Prazo Prescricional"),
    ("certidaoDividaAtiva",                          "[07] Certidão de Dívida Ativa"),
    ("situacaoTipoCobrancaDividaAtiva",              "[08] Situação do Tipo de Cobrança de Dívida Ativa"),
    ("revisaoValorLancamentosCreditosTributarios",   "[09] Revisão de Valor de Lançamentos"),
    ("baixasCreditosTributarios",                    "[10] Baixa dos Créditos Tributários"),
    ("diarioGeralArrecadacao",                       "[11] Diário Geral de Arrecadação"),
    ("estornoReceitasDiarioGeralArrecadacao",        "[12] Estorno/Restituição de Receita"),
]

# ── Etapas ────────────────────────────────────────────────────────────────────
ETAPAS = [
    {"numero": 1, "nome": "Gerar Dados (Todos os Assuntos)",                   "identificador": "d0463029-0757-4972-b8a3-9648d01a3a00", "multi": True,  "gera_zip": None},
    {"numero": 2, "nome": "Tratamento de Dados - Lançamentos Não Encontrados", "identificador": "80245d85-b749-4ea1-b1df-88ee07493b3e", "multi": False, "gera_zip": None, "ignora_erros_se_sucesso": True},
    {"numero": 3, "nome": "Validação de Vínculos Saldos Iniciais",             "identificador": "bf75ff5e-4035-4adb-ad08-c7363811d3ff", "multi": False, "gera_zip": False},
    {"numero": 4, "nome": "Validação de Revisões",                             "identificador": "1b375a6e-000a-41a4-95b1-6e961a237c75", "multi": False, "gera_zip": False},
    {"numero": 5, "nome": "Validação de Cobrança Dívida Ativa",                "identificador": "02d1a518-a662-490e-aa96-a4de13169e57", "multi": False, "gera_zip": None},
    {"numero": 6, "nome": "Validação de Lançamentos",                          "identificador": "89c1ef52-6989-4020-a3f8-65e5fcbb1980", "multi": False, "gera_zip": None},
    {"numero": 7, "nome": "Validação de Imóveis",                              "identificador": "6bf03f6d-3734-4e9a-b984-6aed245733ff", "multi": False, "gera_zip": None},
    {"numero": 8, "nome": "Validação de Contribuintes",                        "identificador": "173efc4a-59b2-4c5e-afd6-1f2f5825172f", "multi": False, "gera_zip": None},
    {"numero": 9, "nome": "Validação de CONs",                                 "identificador": "b0dac6a0-0ea0-48c7-936e-f06e88360687", "multi": False, "gera_zip": None},
]


def _build_payload_etapa01(assunto_valor, competencia):
    return {
        "parametros": {
            "assunto": assunto_valor, "competencia": competencia,
            "tipoEnvio": "MANUAL", "validarGerar": "1",
            "opcaoAbertosInscritos": "TODOS", "intervaloLancamento": "1",
            "limparDadosExistentes": "SIM", "tipoLancamento": "TODOS",
            "tipoBaixa": "TODOS", "enviarTodosAssuntos": "NAO",
            "descarte": "NAO", "usuario": "gerarDados", "senha": "gerarDados",
        }
    }


def _build_payload_simples(competencia):
    return {"parametros": {"competencia": competencia}}


def _build_payload_etapa09(competencia):
    return {"parametros": {"CONs": "[]", "competencia": competencia, "tipoCON": "IMPEDITIVA"}}


def _build_payload_etapa02(competencia):
    return {
        "parametros": {
            "competencia": competencia,
            "p_assuntos": '["saldosIniciaisCreditoTributario"]',
            "p_tipoExecucao": "CORRIGIR_REGISTRO_SALDO",
        }
    }


# ── Classe principal ──────────────────────────────────────────────────────────
class EsfingeExecutor:
    """Executa as 9 etapas do e-Sfinge via API, sem interface gráfica."""

    def __init__(self, authorization, user_access, competencia,
                 log_callback, status_callback,
                 etapa_inicial=1, assunto_inicial=0,
                 visibilidade_publica=True):
        self.authorization      = authorization
        self.user_access        = user_access
        self.competencia        = competencia
        self.etapa_inicial      = etapa_inicial
        self.assunto_inicial    = assunto_inicial
        self.visibilidade_publica = visibilidade_publica
        self._log     = log_callback
        self._status  = status_callback
        self._stop    = False
        self.relatorio = {
            "inicio": datetime.now().isoformat(),
            "competencia": competencia,
            "etapas_executadas": [],
            "etapa_erro": None,
            "erro_detalhes": None,
            "fim": None,
            "status": "em_execucao",
        }

    def stop(self):
        self._stop = True

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "User-Access": self.user_access,
            "Authorization": f"bearer {self.authorization}",
        }

    # ── POST de execução ──────────────────────────────────────────────────────
    def executar_post(self, identificador, payload):
        url = (f"{BASE_URL}/{identificador}/executar/"
               f"?visibilidadeExecucaoPublica={'true' if self.visibilidade_publica else 'false'}")
        body = dict(payload)
        body["visibilidadeExecucaoPublica"] = self.visibilidade_publica
        body["enviarEmailFinalizar"] = False
        body["emailsParaNotificar"]  = []
        last_error = None
        for tentativa in range(1, MAX_NETWORK_RETRIES + 1):
            try:
                resp = requests.post(url, headers=self._headers(), json=body, timeout=120)
                if resp.status_code != 200:
                    raise RuntimeError(f"POST HTTP {resp.status_code}: {resp.text[:500]}")
                data = resp.json()
                protocolo = data.get("codigoExecucao")
                if not protocolo:
                    raise ValueError(f"Sem codigoExecucao: {json.dumps(data)[:500]}")
                return protocolo
            except requests.exceptions.RequestException as e:
                last_error = e
                if tentativa < MAX_NETWORK_RETRIES:
                    self._log(f"  ⚠️ Rede (tentativa {tentativa}/{MAX_NETWORK_RETRIES}): {e}")
                    time.sleep(10)
        raise RuntimeError(f"Falha após {MAX_NETWORK_RETRIES} tentativas: {last_error}")

    # ── Consulta de status ────────────────────────────────────────────────────
    def consultar_status(self, protocolo):
        url = f"{STATUS_URL}/{protocolo}"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=120)
            if resp.status_code == 200:
                data = resp.json()
                if not data.get("concluida", False):
                    return "processando", False
                tc = data.get("tipoConclusao", {})
                valor = tc.get("value", "") if isinstance(tc, dict) else str(tc)
                if valor in ("CANCELADO", "ERRO", "FALHA"):
                    return "erro", False
                return "concluida", data.get("gerouResultado", False)
            if resp.status_code in (401, 403):
                return "erro", False
            if resp.status_code == 404:
                return "processando", False
            if resp.status_code >= 500:
                self._log(f"     ⚠️ Servidor HTTP {resp.status_code} (retentando)")
                return "processando", False
            return "erro", False
        except requests.exceptions.Timeout:
            self._log("     ⚠️ Timeout na consulta (retentando)")
            return "processando", False
        except requests.exceptions.ConnectionError:
            self._log("     ⚠️ Conexão perdida (retentando)")
            return "processando", False
        except Exception:
            return "erro", False

    # ── Download do ZIP ───────────────────────────────────────────────────────
    def baixar_resultado(self, protocolo):
        url = f"{DOWNLOAD_URL}/{protocolo}/resultado"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=180)
            if resp.status_code == 200 and len(resp.content) > 0:
                return resp.content
            self._log(f"     ⚠️ Download HTTP {resp.status_code}")
            return None
        except requests.exceptions.RequestException as e:
            self._log(f"     ⚠️ Erro ao baixar: {e}")
            return None

    # ── Aguardar resultado ────────────────────────────────────────────────────
    def aguardar_resultado(self, protocolo, descricao):
        inicio = time.time()
        tentativa = 0
        while not self._stop:
            elapsed = int(time.time() - inicio)
            tentativa += 1
            m, s = divmod(elapsed, 60)
            self._log(f"  ⏳ [{tentativa}] Consultando '{descricao}' ({m}m{s}s)...")
            status, gerou = self.consultar_status(protocolo)
            if status == "concluida":
                if gerou:
                    self._log(f"  📦 Concluído com resultado — baixando...")
                    content = self.baixar_resultado(protocolo)
                    if content:
                        return content
                    self._log("     ⚠️ Falha no download, retentando...")
                    time.sleep(5)
                    return self.baixar_resultado(protocolo)
                self._log(f"  📋 Concluído sem arquivo de resultado")
                return None
            if status == "erro":
                raise RuntimeError(f"Execução de '{descricao}' retornou ERRO (protocolo: {protocolo})")
            time.sleep(POLLING_INTERVAL)
        raise InterruptedError("Interrompido pelo usuário")

    # ── Verificação de erros no ZIP ───────────────────────────────────────────
    @staticmethod
    def is_zip(data):
        return data is not None and len(data) >= 4 and data[:4] == b'PK\x03\x04'

    def verificar_erros_zip(self, zip_content):
        erros = []
        try:
            with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
                for nome in zf.namelist():
                    if not nome.endswith(".html"):
                        continue
                    html = zf.read(nome).decode("utf-8", errors="replace")
                    # Estratégia 1: IDs qtdErroN
                    total = sum(int(m) for m in re.findall(r'id="qtdErro\d*"[^>]*>(\d+)', html))
                    if total > 0:
                        erros.append(f"[{nome}] {total} erro(s)")
                        for desc_html in re.findall(
                            r'class="table-danger"[^>]*>.*?<td[^>]*>Erro</td>\s*<td[^>]*>(.*?)</td>',
                            html, re.DOTALL
                        )[:10]:
                            desc = re.sub(r'<[^>]+>', ' ', desc_html).strip()
                            desc = re.sub(r'\s+', ' ', desc)[:200]
                            if desc:
                                erros.append(f"  → {desc}")
                        continue
                    # Estratégia 2
                    if html.count('class="table-danger"') > 0:
                        erros.append(f"[{nome}] erro(s) detectado(s)")
                        continue
                    # Estratégia 3
                    if re.search(r'<td[^>]*>\s*Erro\s*</td>', html):
                        erros.append(f"[{nome}] erro(s) na tabela")
        except zipfile.BadZipFile:
            erros.append("CRÍTICO: ZIP inválido ou corrompido")
        except Exception as e:
            erros.append(f"CRÍTICO: Falha ao analisar ZIP — {e}")
        return len(erros) > 0, erros

    def _zip_tem_correcoes_sucesso(self, zip_content):
        try:
            with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
                for nome in zf.namelist():
                    if nome.endswith(".html"):
                        html = zf.read(nome).decode("utf-8", errors="replace")
                        if "foram corrigidos com sucesso" in html:
                            return True
        except Exception:
            pass
        return False

    # ── Etapa 01 (multi-assunto) ──────────────────────────────────────────────
    def executar_etapa_01(self, etapa):
        payloads = [
            (desc, _build_payload_etapa01(val, self.competencia))
            for val, desc in ASSUNTOS_ETAPA_01
        ]
        if self.assunto_inicial > 0:
            self._log(f"  ⏭️ Pulando assuntos 0 a {self.assunto_inicial - 1}")
            payloads = payloads[self.assunto_inicial:]
            self.assunto_inicial = 0

        self._log("📋 Fase 1 — Disparando POSTs...")
        protocolos = []
        for desc, payload in payloads:
            if self._stop:
                raise InterruptedError("Interrompido pelo usuário")
            self._log(f"  🚀 Disparando: {desc}")
            protocolo = self.executar_post(etapa["identificador"], payload)
            self._log(f"     Protocolo: {protocolo}")
            protocolos.append((desc, protocolo))

        self._log(f"\n📋 Fase 2 — Monitorando {len(protocolos)} resultados...")
        todos_erros = []
        for idx, (desc, protocolo) in enumerate(protocolos):
            if self._stop:
                raise InterruptedError("Interrompido pelo usuário")
            self._log(f"\n  [{idx+1}/{len(protocolos)}] Aguardando: {desc}")
            content = self.aguardar_resultado(protocolo, desc)
            if content and self.is_zip(content):
                tem_erro, erros = self.verificar_erros_zip(content)
                if tem_erro:
                    todos_erros.extend(erros)
                    self._log(f"  🔴 ERRO em {desc}:")
                    for e in erros:
                        self._log(f"     {e}")
                else:
                    self._log(f"  ✅ {desc} — sem erros")
            else:
                self._log(f"  ℹ️ Sem resultado para {desc}")

        if todos_erros:
            raise RuntimeError(
                f"Etapa 01: {len(todos_erros)} erro(s):\n" + "\n".join(todos_erros[:30])
            )
        self._log("\n✅ Etapa 01 — todos os 13 assuntos sem erros.")

    # ── Etapas simples (2-9) ──────────────────────────────────────────────────
    def executar_etapa_simples(self, etapa):
        num = etapa["numero"]
        if num == 2:
            payload = _build_payload_etapa02(self.competencia)
        elif num == 9:
            payload = _build_payload_etapa09(self.competencia)
        else:
            payload = _build_payload_simples(self.competencia)

        self._log("  🚀 Disparando POST...")
        protocolo = self.executar_post(etapa["identificador"], payload)
        self._log(f"     Protocolo: {protocolo}")

        content = self.aguardar_resultado(protocolo, etapa["nome"])

        if content and self.is_zip(content):
            tem_erro, erros = self.verificar_erros_zip(content)
            if tem_erro:
                self._log("  🔴 ERROS encontrados:")
                for e in erros:
                    self._log(f"     {e}")
                if etapa.get("ignora_erros_se_sucesso") and self._zip_tem_correcoes_sucesso(content):
                    self._log("  ⚠️ Os erros indicam registros CORRIGIDOS com sucesso.")
                    self._log(f"  ✅ Etapa {num:02d} — correções aplicadas.")
                else:
                    raise RuntimeError(
                        f"Etapa {num:02d} ({etapa['nome']}): {len(erros)} erro(s)"
                    )
            else:
                self._log("  ✅ Resultado verificado — ZERO erros.")
        else:
            if etapa["gera_zip"] is False:
                self._log("  ✅ Execução concluída (etapa não gera arquivo).")
            elif etapa["gera_zip"] is None:
                self._log("  ✅ Concluído — nenhuma inconsistência encontrada.")
            else:
                raise RuntimeError(
                    f"Etapa {num:02d}: esperava ZIP mas nenhum foi retornado."
                )

    # ── Execução principal ────────────────────────────────────────────────────
    def executar(self):
        try:
            etapas = [e for e in ETAPAS if e["numero"] >= self.etapa_inicial]
            if self.etapa_inicial > 1:
                self._log(f"\n⏭️ Iniciando na etapa {self.etapa_inicial:02d}")

            for etapa in etapas:
                if self._stop:
                    raise InterruptedError("Interrompido pelo usuário")
                num  = etapa["numero"]
                nome = etapa["nome"]
                self._status(f"Etapa {num:02d} — {nome}")
                self._log(f"\n{'='*60}")
                self._log(f"🔄 ETAPA {num:02d} - {nome}")
                self._log(f"   Identificador: {etapa['identificador']}")
                self._log(f"{'='*60}")

                if etapa.get("multi"):
                    self.executar_etapa_01(etapa)
                else:
                    self.executar_etapa_simples(etapa)

                self.relatorio["etapas_executadas"].append(
                    {"numero": num, "nome": nome, "status": "sucesso"}
                )
                self._log(f"\n   ══ Etapa {num:02d} OK — avançando ══")

            self.relatorio["status"] = "concluido_sucesso"
            self._log(f"\n{'='*60}")
            self._log("🎉 TODAS AS ETAPAS CONCLUÍDAS COM SUCESSO!")
            self._log(f"{'='*60}")

        except InterruptedError as e:
            self.relatorio["status"]        = "interrompido"
            self.relatorio["erro_detalhes"] = str(e)
            self._log(f"\n⚠️ {e}")

        except Exception as e:
            self.relatorio["status"]        = "erro"
            idx = len(self.relatorio["etapas_executadas"])
            self.relatorio["etapa_erro"]    = ETAPAS[idx]["numero"] if idx < len(ETAPAS) else -1
            self.relatorio["erro_detalhes"] = str(e)
            self._log(f"\n{'='*60}")
            self._log(f"🔴 EXECUÇÃO PARADA")
            self._log(f"   {e}")
            self._log(f"{'='*60}")

        finally:
            self.relatorio["fim"] = datetime.now().isoformat()

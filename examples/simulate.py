"""Real end-to-end simulations of the reparo_luminaria flow.

Drives the compiled LangGraph flow as realistic citizen conversations, over the
REAL HTTP backends (a deterministic geocoder + SGRC stub via httpx.MockTransport
— no network), printing a turn-by-turn transcript.

Run:  uv run python examples/simulate.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from flowspec2 import FlowRuntime, load_flow
from flowspec2.backends import BackendConfig, make_registry

DOC = load_flow(str(Path(__file__).with_name("reparo_luminaria.flow.json")))
CONFIG = BackendConfig(
    geocode_url="https://services.example/geocode",
    cpf_lookup_url="https://services.example/cpf",
    govbr_enrich_url="https://services.example/govbr",
    sgrc_url="https://services.example/sgrc",
    api_key="demo-token",
)

RESET = "\033[0m"; DIM = "\033[2m"; BOLD = "\033[1m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; CYAN = "\033[36m"


def make_transport(sgrc) -> httpx.MockTransport:
    """A deterministic stand-in for the Prefeitura services."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/geocode"):
            address = json.loads(request.content or b"{}").get("address", "")
            return httpx.Response(200, json={"logradouro": address, "bairro": "Centro", "municipio": "Rio de Janeiro"})
        if path.endswith("/cpf"):
            return httpx.Response(200, json={"name": "", "email": "", "phones": []})
        if path.endswith("/govbr"):
            return httpx.Response(200, json={"phones": ["5521988887777"]})
        if path.endswith("/sgrc"):
            return sgrc(request)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def runtime(sgrc) -> FlowRuntime:
    return FlowRuntime(DOC, tools=make_registry(CONFIG, transport=make_transport(sgrc)))


def _render(state) -> None:
    ar = state.agent_response
    iv = ar.interactive or {}
    print(f"  {CYAN}🤖{RESET} {ar.description}")
    if iv.get("status") == "flow_sent":
        print(f"     {DIM}↳ [enviou o formulário WhatsApp Flow para preenchimento]{RESET}")
    elif iv.get("out_of_band_sent"):
        print(f"     {DIM}↳ [enviou o botão de login gov.br — aguardando autenticação]{RESET}")
    elif iv.get("buttons"):
        print(f"     {DIM}↳ botões: {' · '.join(b['title'] for b in iv['buttons'])}{RESET}")
    elif iv.get("sections"):
        rows = [r["title"] for s in iv["sections"] for r in s["rows"]]
        print(f"     {DIM}↳ lista: {' · '.join(rows)}{RESET}")
    if state.status == "completed" and state.data.get("protocol_id"):
        print(f"     {GREEN}✅ chamado aberto — protocolo {state.data['protocol_id']}{RESET}")
    elif state.status == "error":
        print(f"     {YELLOW}⚠️  {ar.error_message}{RESET}")


class Sim:
    def __init__(self, rt: FlowRuntime, user: str = "5521999990000") -> None:
        self.rt = rt
        self.user = user
        self.state = rt.new_state(user)

    async def say(self, human: str, payload: dict) -> None:
        print(f"  👤 {human}")
        self.state = await self.rt.execute(self.state, payload)
        _render(self.state)
        print()


def banner(title: str) -> None:
    print(f"\n{BOLD}━━━ {title} ━━━{RESET}\n")


async def scenario_flow_happy():
    banner("Cenário 1 — caminho de produção (WhatsApp Flow), cidadão anônimo")
    sim = Sim(runtime(lambda r: httpx.Response(201, json={"protocolo": "RLU-2026-000481"})))
    await sim.say("a luz da minha rua tá apagada faz dias", {})
    await sim.say("[preenche o formulário]", {"_source": "whatsapp_flow", "defect_type": "Apagada", "qty_pattern": "bloco", "location": "Rua"})
    await sim.say("Rua das Laranjeiras, 300", {"address": "Rua das Laranjeiras, 300"})
    await sim.say("isso, pode confirmar", {"confirmacao": "sim"})
    await sim.say("em frente ao mercadinho", {"ponto_referencia": "em frente ao mercadinho"})
    await sim.say("prefiro não me identificar", {"identification_method": "anonimo"})
    await sim.say("pode abrir 👍", {"confirmacao": "sim"})


async def scenario_correction():
    banner("Cenário 2 — cidadão corrige o endereço na confirmação")
    sim = Sim(runtime(lambda r: httpx.Response(201, json={"protocolo": "RLU-2026-000482"})))
    await sim.say("o poste tá piscando", {})
    await sim.say("[preenche o formulário]", {"_source": "whatsapp_flow", "defect_type": "Piscando", "qty_pattern": "uma", "location": "Rua"})
    await sim.say("Rua Barata Ribeiro, 200", {"address": "Rua Barata Ribeiro, 200"})
    await sim.say("sim", {"confirmacao": "sim"})
    await sim.say("perto da farmácia", {"ponto_referencia": "perto da farmácia"})
    await sim.say("anônimo", {"identification_method": "anonimo"})
    await sim.say("não, o endereço está errado", {"correcao": "endereço"})
    await sim.say("é na Rua Tonelero, 150", {"address": "Rua Tonelero, 150"})
    await sim.say("agora sim", {"confirmacao": "sim"})
    await sim.say("na esquina com a Siqueira Campos", {"ponto_referencia": "esquina com a Siqueira Campos"})
    await sim.say("pode abrir", {"confirmacao": "sim"})


async def scenario_govbr():
    banner("Cenário 3 — identificação via gov.br (out-of-band)")
    sim = Sim(runtime(lambda r: httpx.Response(201, json={"protocolo": "RLU-2026-000483"})))
    await sim.say("luminária danificada na minha rua", {})
    await sim.say("[preenche o formulário]", {"_source": "whatsapp_flow", "defect_type": "Danificada", "location": "Rua"})
    await sim.say("Av. Atlântica, 1700", {"address": "Av. Atlântica, 1700"})
    await sim.say("sim", {"confirmacao": "sim"})
    await sim.say("ao lado do quiosque", {"ponto_referencia": "ao lado do quiosque"})
    await sim.say("quero usar o gov.br", {"identification_method": "govbr"})
    await sim.say("[faz login no gov.br e volta]", {"govbr_token": {"cpf": "52998224725", "nome": "Joana Ribeiro", "email": "joana@example.com"}})
    await sim.say("pode confirmar", {"confirmacao": "sim"})


async def scenario_praca_quadra():
    banner("Cenário 4 — endereço em praça abre a pergunta de quadra de esportes")
    sim = Sim(runtime(lambda r: httpx.Response(201, json={"protocolo": "RLU-2026-000484"})))
    await sim.say("as luzes da praça estão apagadas", {})
    await sim.say("[preenche o formulário]", {"_source": "whatsapp_flow", "defect_type": "Apagada", "qty_pattern": "intercaladas", "location": "Praça"})
    await sim.say("Praça General Osório", {"address": "Praça General Osório"})
    await sim.say("sim", {"confirmacao": "sim"})
    await sim.say("sim, é dentro da quadra", {"reparo_luminaria_quadra_esportes": "sim"})
    await sim.say("perto da rede de vôlei", {"ponto_referencia": "perto da rede de vôlei"})
    await sim.say("anônimo", {"identification_method": "anonimo"})
    await sim.say("pode abrir", {"confirmacao": "sim"})


async def scenario_sgrc_retry():
    banner("Cenário 5 — SGRC fora do ar (503 → retryable) e depois recupera")
    calls = {"n": 0}

    def flaky(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="SGRC indisponível")
        return httpx.Response(201, json={"protocolo": "RLU-2026-000485"})

    sim = Sim(runtime(flaky))
    await sim.say("luz apagada na rua", {})
    await sim.say("[preenche o formulário]", {"_source": "whatsapp_flow", "defect_type": "Apagada", "qty_pattern": "uma", "location": "Rua"})
    await sim.say("Rua Sá Ferreira, 40", {"address": "Rua Sá Ferreira, 40"})
    await sim.say("sim", {"confirmacao": "sim"})
    await sim.say("perto do ponto de ônibus", {"ponto_referencia": "perto do ponto de ônibus"})
    await sim.say("anônimo", {"identification_method": "anonimo"})
    await sim.say("pode abrir", {"confirmacao": "sim"})           # 1st open -> 503 retryable
    await sim.say("tenta de novo por favor", {"confirmacao": "sim"})  # retry -> success, state preserved
    print(f"  {DIM}(chamadas ao SGRC: {calls['n']} — estado preservado entre a falha e o retry){RESET}\n")


async def scenario_idempotency():
    banner("Cenário 6 — idempotência: reenvio duplicado não abre 2 chamados")
    calls = {"n": 0}

    def counting(_request):
        calls["n"] += 1
        return httpx.Response(201, json={"protocolo": "RLU-2026-000486"})

    rt = runtime(counting)  # one runtime/registry => shared replay cache
    answers = [
        ("luz apagada", {}),
        ("[formulário]", {"_source": "whatsapp_flow", "defect_type": "Apagada", "qty_pattern": "uma", "location": "Rua"}),
        ("Rua Pompeu Loureiro, 5", {"address": "Rua Pompeu Loureiro, 5"}),
        ("sim", {"confirmacao": "sim"}),
        ("perto da escola", {"ponto_referencia": "perto da escola"}),
        ("anônimo", {"identification_method": "anonimo"}),
        ("pode abrir", {"confirmacao": "sim"}),
    ]
    print(f"  {DIM}— primeira conversa —{RESET}")
    sim1 = Sim(rt, user="5521900000001")
    for _, payload in answers:
        sim1.state = await rt.execute(sim1.state, payload)
    print(f"  👤 (conversa completa) → {GREEN}protocolo {sim1.state.data['protocol_id']}{RESET}")

    print(f"  {DIM}— mesmo cidadão, MESMAS respostas (reenvio duplicado) —{RESET}")
    sim2 = Sim(rt, user="5521900000001")
    for _, payload in answers:
        sim2.state = await rt.execute(sim2.state, payload)
    print(f"  👤 (conversa completa) → {GREEN}protocolo {sim2.state.data['protocol_id']}{RESET}")
    print(f"  {BOLD}→ chamadas reais ao SGRC: {calls['n']} (idempotência: o 2º reusa o protocolo){RESET}\n")


async def main():
    print(f"{BOLD}flowspec2 · simulações reais — reparo_luminaria sobre backends HTTP (MockTransport){RESET}")
    await scenario_flow_happy()
    await scenario_correction()
    await scenario_govbr()
    await scenario_praca_quadra()
    await scenario_sgrc_retry()
    await scenario_idempotency()
    print(f"{GREEN}{BOLD}✓ todas as simulações concluídas{RESET}")


if __name__ == "__main__":
    asyncio.run(main())

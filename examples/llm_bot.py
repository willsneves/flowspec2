"""LLM-driven simulation — the bot's *intelligence* doing the non-deterministic work.

Here the citizen speaks in messy free text. A real LLM (Gemini 2.5 Flash, the
the configured model) does the non-deterministic part:
routing into the flow and extracting the closed token for each slot from the
node's payload_schema / interactive options. flowspec2 holds the rails — the
validators reject anything out of domain. The transcript shows the boundary:

    👤 free text   →   🧠 LLM extraction (closed token)   →   🤖 the rail's next state

Uses the conversational variant of reparo_luminaria (no auto_flow / no summary)
so the LLM extracts EVERY slot from natural language. Needs GEMINI_API_KEY.

Run:  uv run python examples/llm_bot.py
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from pathlib import Path

from flowspec2 import FlowRuntime, load_flow
from flowspec2.llm import GeminiAgent

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAG = "\033[35m"

BASE = load_flow(str(Path(__file__).with_name("reparo_luminaria.flow.json")))


def conversational(doc: dict) -> dict:
    """Strip auto_flow + the summary step so the LLM drives every slot in text."""
    d = copy.deepcopy(doc)
    d.pop("auto_flow", None)
    d["path"] = [s for s in d["path"] if s.get("confirm") != "service_confirmed"]
    return d


def _render(state) -> None:
    ar = state.agent_response
    iv = ar.interactive or {}
    print(f"  {CYAN}🤖{RESET} {ar.description}")
    if iv.get("out_of_band_sent"):
        print(f"     {DIM}↳ [enviou o botão de login gov.br]{RESET}")
    elif iv.get("buttons"):
        print(f"     {DIM}↳ botões: {' · '.join(b['title'] for b in iv['buttons'])}{RESET}")
    elif iv.get("sections"):
        print(
            f"     {DIM}↳ lista: {' · '.join(r['title'] for s in iv['sections'] for r in s['rows'])}{RESET}"
        )
    if state.status == "completed" and state.data.get("protocol_id"):
        print(f"     {GREEN}✅ chamado aberto — protocolo {state.data['protocol_id']}{RESET}")
    elif state.status == "error":
        print(f"     {YELLOW}⚠️  {ar.error_message}{RESET}")


def banner(title: str) -> None:
    print(f"\n{BOLD}━━━ {title} ━━━{RESET}\n")


async def converse(agent: GeminiAgent, title: str, opener: str, turns: list) -> None:
    banner(title)
    rt = FlowRuntime(conversational(BASE))
    state = rt.new_state("5521999990000")

    print(f"  👤 {opener}")
    service = await asyncio.to_thread(agent.route, opener, [rt.doc])
    print(f"  {MAG}🧭 roteou para: {service}{RESET}")
    if service != rt.flow:
        print(
            f"  {CYAN}🤖{RESET} Desculpe, ainda não consigo ajudar com isso. Posso registrar problemas de iluminação pública. 🙏\n"
        )
        return
    state = await rt.execute(state, {})
    _render(state)
    print()

    for turn in turns:
        if state.status == "completed":
            break
        if isinstance(turn, dict):  # an external signal (e.g. gov.br auth completion)
            print(f"  {DIM}📨 [sinal externo: {', '.join(turn)}]{RESET}")
            payload = turn
        else:
            print(f"  👤 {turn}")
            payload = await asyncio.to_thread(agent.extract, turn, state.agent_response)
            print(f"  {MAG}🧠 extraiu: {json.dumps(payload, ensure_ascii=False)}{RESET}")
        state = await rt.execute(state, payload)
        _render(state)
        print()


async def main() -> None:
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY ausente — defina a chave para rodar o bot com LLM.")
        sys.exit(1)
    agent = GeminiAgent()
    print(f"{BOLD}flowspec2 · bot com inteligência de LLM (Gemini) — reparo_luminaria{RESET}")
    print(
        f"{DIM}cidadão fala em texto livre; o LLM roteia e extrai o token fechado; o flowspec2 valida o trilho.{RESET}"
    )

    await converse(
        agent,
        "Conversa 1 — texto livre, caminho feliz (anônimo)",
        "oi! a iluminação da minha rua tá com problema, queria reportar",
        [
            "a luz tá apagada faz uns dias, fica tudo escuro à noite 😞",
            "é o quarteirão inteiro, várias apagadas",
            "umas acendem e outras não",
            "é na rua mesmo",
            "Rua das Laranjeiras, 300, Laranjeiras",
            "isso, pode confirmar 👍",
            "tem um mercadinho bem em frente",
            "não precisa, prefiro ficar anônimo",
            "pode abrir sim, obrigado",
        ],
    )

    await converse(
        agent,
        "Conversa 2 — cidadão pede correção em linguagem natural",
        "preciso reportar um poste com problema",
        [
            "o poste fica piscando direto",
            "é só esse mesmo, uma luminária",
            "fica na rua",
            "Rua Barata Ribeiro, 200, Copacabana",
            "sim",
            "perto da farmácia",
            "anônimo pode ser",
            "opa, espera, acho que falei o endereço errado",  # -> correcao endereço
            "é na Rua Tonelero, 150 na verdade",
            "agora sim",
            "do lado da banca de jornal",
            "pode abrir",
        ],
    )

    await converse(
        agent,
        "Conversa 3 — identificação via gov.br",
        "quero abrir um chamado, a luminária tá danificada",
        [
            "tá toda quebrada, pendurada no poste",
            "é na rua",
            "Av. Atlântica, 1700, Copacabana",
            "sim",
            "ao lado do quiosque",
            "quero me identificar pelo gov.br",
            {
                "govbr_token": {
                    "cpf": "52998224725",
                    "nome": "Joana Ribeiro",
                    "email": "joana@example.com",
                }
            },
            "pode confirmar",
        ],
    )

    await converse(
        agent,
        "Conversa 4 — praça abre a pergunta de quadra de esportes",
        "as luzes da praça aqui estão apagadas",
        [
            "tá tudo apagado",
            "são várias luminárias",
            "umas acendem e outras não",
            "é numa praça",
            "Praça General Osório, Ipanema",
            "sim",
            "sim, é dentro da quadra de esportes",
            "perto da rede de vôlei",
            "anônimo",
            "pode abrir",
        ],
    )

    await converse(
        agent,
        "Conversa 5 — roteamento nega serviço fora de escopo",
        "oi, eu queria pagar o meu IPTU atrasado",
        [],
    )

    print(f"{GREEN}{BOLD}✓ simulações com LLM concluídas{RESET}")


if __name__ == "__main__":
    asyncio.run(main())

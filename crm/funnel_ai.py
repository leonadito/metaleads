"""The funnel built by the LLM from the client's profile (Phase 7; decisions
of 2026-10-14 and 2026-10-15).

When the AI is turned on for a client (profile complete), the LLM writes the
master prompt of each of the 6 stages (what the stage means for this
business) and creates the sub-stages of each stage, up to 5 active, each
with a prompt explaining why it exists and when a lead belongs there. It
goes through the same functions as people (crm.funnel), so the rules hold:
locked prompts are never rewritten, the limit of 5 is respected, existing
sub-stages are kept (their unlocked prompts may be improved), nothing is
renamed or deactivated. The profile version used is recorded.
"""
import json
import logging

from django.db import transaction
from django.utils import timezone
from django_tasks import task

from . import funnel, llm
from .models import DEFAULT_STAGE_PROMPTS, MAX_SUBSTAGES_PER_STAGE, Stage, StagePrompt, Substage, Tenant

logger = logging.getLogger(__name__)

SYSTEM = f"""Você monta o funil de vendas de um cliente no CRM da Agitare, uma agência de marketing digital que capta,
qualifica e atende leads de qualquer tipo de negócio.

O funil tem 6 etapas fixas, que não mudam. O significado genérico de cada uma é:
{chr(10).join(f"- {Stage(k).label}: {v}" for k, v in DEFAULT_STAGE_PROMPTS.items())}

Sua tarefa, a partir do perfil do cliente:
1. Para cada etapa, escreva o prompt mestre: o que aquela etapa significa para o negócio deste cliente, em 2 a 4
   frases objetivas. Ex.: o que é "qualificado" para esta empresa, com os critérios que o perfil dá.
2. Para cada etapa, crie as subetapas que ajudam a encaminhar o lead da melhor forma, no máximo
   {MAX_SUBSTAGES_PER_STAGE} por etapa. Crie só as úteis para este negócio; uma etapa pode não ter nenhuma.
   - Nome curto (até 40 caracteres), claro para a equipe de vendas.
   - Prompt da subetapa: por que ela existe e quando um lead deve estar nela, em 1 a 3 frases. Ele detalha o prompt
     mestre da etapa e nunca o contradiz.
   - As subetapas de uma etapa não se sobrepõem: um lead tem que caber em uma só.
3. Use só o que o perfil diz. Não invente preços, prazos, produtos, nomes ou regras que não estejam lá.
4. Escreva em português do Brasil, no tom de uma orientação interna para a equipe e para a IA de atendimento.

Subetapas que já existem vêm listadas. Mantenha-as (repita o nome exato) e, se puder, melhore o prompt delas; as
marcadas como bloqueadas não podem ter o prompt alterado: repita o prompt atual sem mudanças. Prompts mestres
bloqueados também ficam iguais."""

SCHEMA = {
    "type": "object",
    "properties": {
        stage: {
            "type": "object",
            "properties": {
                "prompt_mestre": {"type": "string"},
                "subetapas": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"nome": {"type": "string"}, "prompt": {"type": "string"}},
                        "required": ["nome", "prompt"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["prompt_mestre", "subetapas"],
            "additionalProperties": False,
        }
        for stage in Stage.values
    },
    "required": list(Stage.values),
    "additionalProperties": False,
}


def profile_text(profile):
    labels = profile.get("labels") or {}
    lines = []
    for key, value in (profile.get("answers") or {}).items():
        if key == "faq":
            pairs = "; ".join(f"P: {i.get('q', '')} R: {i.get('a', '')}" for i in value if isinstance(i, dict))
            lines.append(f"- Perguntas frequentes: {pairs}")
        elif isinstance(value, list):
            lines.append(f"- {labels.get(key, key)}: {', '.join(map(str, value))}")
        else:
            lines.append(f"- {labels.get(key, key)}: {value}")
    return "\n".join(lines)


def current_funnel_text(tenant):
    masters = funnel.stage_prompts(tenant)
    blocks = []
    for value, label in Stage.choices:
        m = masters[value]
        lines = [f"{label}:",
                 f"  prompt mestre {'(BLOQUEADO) ' if m['locked'] else ''}atual: {m['prompt'] if m['by'] != 'system' else '(genérico, pode substituir)'}"]
        for s in Substage.objects.filter(tenant=tenant, stage=value, is_active=True).order_by("order", "id"):
            lines.append(f"  subetapa existente{' (BLOQUEADA)' if s.prompt_locked else ''}: {s.name} — {s.prompt or '(sem prompt)'}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_request(tenant):
    return {
        "system": SYSTEM,
        "max_tokens": 8000,
        "messages": [{"role": "user", "content": (
            f"Perfil do cliente {tenant.name}:\n{profile_text(tenant.profile)}\n\n"
            f"Funil atual:\n{current_funnel_text(tenant)}"
        )}],
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
    }


def apply(tenant, proposal, profile_version):
    """Applies the LLM's proposal through crm.funnel (same rules as people).
    Returns a summary of what changed."""
    done = {"prompts_mestres": 0, "criadas": 0, "atualizadas": 0, "ignoradas": []}
    with transaction.atomic():
        for value in Stage.values:
            part = proposal.get(value) or {}
            text = (part.get("prompt_mestre") or "").strip()
            if text:
                try:
                    funnel.set_stage_prompt(tenant, value, text, by=StagePrompt.LLM, profile_version=profile_version)
                    done["prompts_mestres"] += 1
                except funnel.FunnelError as exc:
                    done["ignoradas"].append(f"{Stage(value).label}: {exc}")
            existing = {s.name.casefold(): s for s in Substage.objects.filter(tenant=tenant, stage=value)}
            for item in (part.get("subetapas") or [])[:MAX_SUBSTAGES_PER_STAGE]:
                name, prompt = (item.get("nome") or "").strip()[:100], (item.get("prompt") or "").strip()
                if not name:
                    continue
                sub = existing.get(name.casefold())
                try:
                    if sub is None:
                        funnel.create_substage(tenant, value, name, prompt=prompt, by=Substage.LLM)
                        done["criadas"] += 1
                    elif sub.is_active and not sub.prompt_locked and prompt and prompt != sub.prompt:
                        funnel.update_substage(sub, prompt=prompt, by=Substage.LLM)
                        done["atualizadas"] += 1
                except funnel.FunnelError as exc:
                    done["ignoradas"].append(f"{Stage(value).label} > {name}: {exc}")
        Tenant.objects.filter(pk=tenant.pk).update(ai_funnel_built_at=timezone.now(), ai_funnel_profile_version=profile_version,
                                                   ai_last_error="")
    return done


@task
def build_funnel(tenant_id):
    tenant = Tenant.objects.filter(pk=tenant_id).first()
    if tenant is None or not tenant.ai_enabled_at:
        return "IA desligada"
    profile = tenant.profile or {}
    if not profile.get("ready_for_ai"):
        Tenant.objects.filter(pk=tenant.pk).update(ai_last_error="perfil do cliente incompleto")
        return "perfil incompleto"
    try:
        answer = llm.call(tenant, "sdr", build_request(tenant))
        result = apply(tenant, llm.json_of(answer), profile.get("version"))
    except llm.LLMError as exc:
        Tenant.objects.filter(pk=tenant.pk).update(ai_last_error=str(exc)[:1000])
        logger.warning("Funil de %s não montado pela IA: %s", tenant, exc)
        return f"erro: {exc}"
    logger.info("Funil de %s montado pela IA: %s", tenant, json.dumps(result, ensure_ascii=False))
    return result

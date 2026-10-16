"""CRM screens of the AI (Phase 7): "Documentos da IA" (the client's master RAG)
and the approval of the SDR agent's drafts."""
from urllib.parse import urlencode

from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from . import chatwoot_actions, llm, rag, sdr
from .models import KnowledgeDocument, LeadEvent, ReplyDraft
from .views import actor, tenant_required


def _back(**params):
    return redirect("/crm/ia/documentos/?" + urlencode(params) if params else "/crm/ia/documentos/")


@tenant_required
def documents_page(request):
    tenant = request.tenant
    query, found, search_error = request.GET.get("q", "").strip(), None, ""
    if query:
        try:
            found = rag.search(tenant, query)
        except llm.LLMError as exc:
            search_error = str(exc)
    return render(request, "crm/rag.html", {
        "tenant": tenant,
        "documents": KnowledgeDocument.objects.filter(tenant=tenant),
        "query": query,
        "found": found,
        "search_error": search_error,
        "ok": request.GET.get("ok", ""),
        "error": request.GET.get("erro", ""),
        "max_mb": rag.MAX_FILE_BYTES // (1024 * 1024),
    })


@tenant_required
@require_POST
def document_add(request):
    tenant, kind = request.tenant, request.POST.get("kind")
    title = request.POST.get("title", "").strip()[:200]
    doc = KnowledgeDocument(tenant=tenant, kind=kind, title=title, created_by=actor(request))
    try:
        if kind == KnowledgeDocument.Kind.FILE:
            upload = request.FILES.get("file")
            if upload is None:
                raise rag.RagError("escolha um arquivo")
            if upload.size > rag.MAX_FILE_BYTES:
                raise rag.RagError("arquivo maior que 10 MB")
            doc.source = upload.name[:500]
            doc.text = rag.extract_file(upload.name, upload.read())
            doc.title = title or upload.name.rsplit(".", 1)[0][:200]
            doc.content_type = (upload.content_type or "application/octet-stream")[:100]
            upload.seek(0)
            doc.file.save(upload.name, upload, save=False)
        elif kind == KnowledgeDocument.Kind.URL:
            url = request.POST.get("url", "").strip()
            rag.check_public_url(url)
            doc.source = url[:500]
        elif kind == KnowledgeDocument.Kind.TEXT:
            doc.text = rag.clean_text(request.POST.get("text", ""))
            if len(doc.text) < 20 or not title:
                raise rag.RagError("dê um título e cole o texto")
        else:
            raise Http404
    except rag.RagError as exc:
        return _back(erro=str(exc))
    doc.save()
    rag.index_document.enqueue(doc.pk)
    return _back(ok=f"“{doc.title or doc.source}” entrou na fila; em alguns segundos fica pronto.")


def _document(request, doc_uuid):
    doc = KnowledgeDocument.objects.filter(tenant=request.tenant, uuid=doc_uuid).first()
    if doc is None:
        raise Http404
    return doc


@tenant_required
@require_POST
def document_reindex(request, doc_uuid):
    doc = _document(request, doc_uuid)
    fields = {"status": KnowledgeDocument.Status.PENDING, "error": ""}
    if doc.kind == KnowledgeDocument.Kind.URL:
        fields["text"] = ""  # fetch the page again
    KnowledgeDocument.objects.filter(pk=doc.pk).update(**fields)
    rag.index_document.enqueue(doc.pk)
    return _back(ok=f"“{doc.title}” vai ser reindexado.")


@tenant_required
@require_POST
def document_toggle(request, doc_uuid):
    doc = _document(request, doc_uuid)
    KnowledgeDocument.objects.filter(pk=doc.pk).update(is_active=not doc.is_active)
    return _back(ok=f"“{doc.title}” {'fora do uso' if doc.is_active else 'de volta ao uso'} pela IA.")


@tenant_required
@require_POST
def document_delete(request, doc_uuid):
    doc = _document(request, doc_uuid)
    title = doc.title
    if doc.file:
        doc.file.delete(save=False)
    doc.delete()
    return _back(ok=f"“{title}” apagado.")


@tenant_required
def document_file(request, doc_uuid):
    """The original file, only to people logged in to the client."""
    doc = _document(request, doc_uuid)
    if not doc.file:
        raise Http404
    return FileResponse(doc.file.open("rb"), as_attachment=True, filename=doc.source or "documento",
                        content_type=doc.content_type or "application/octet-stream")


@tenant_required
def draft_page(request, draft_uuid):
    """A draft of the SDR agent: send it (maybe edited) or discard it."""
    draft = ReplyDraft.objects.select_related("lead__contact").filter(tenant=request.tenant, uuid=draft_uuid).first()
    if draft is None:
        raise Http404
    error = ""
    if request.method == "POST" and draft.status == ReplyDraft.Status.DRAFT:
        if request.POST.get("action") == "discard":
            sdr.close(draft, ReplyDraft.Status.DISCARDED, actor(request))
            return redirect(f"/crm/leads/{draft.lead.uuid}/")
        text = request.POST.get("text", "").strip()
        if not text:
            error = "A mensagem está vazia."
        else:
            try:
                sdr.approve(draft, text, actor(request))
                return redirect(f"/crm/leads/{draft.lead.uuid}/")
            except (ValueError, chatwoot_actions.ChatwootUnavailable) as exc:
                error = f"Não foi enviada: {exc}"
    newer = LeadEvent.objects.filter(lead=draft.lead, type="message", data__direction="incoming",
                                     data__message_id__gt=draft.answers_message_id).exists()
    return render(request, "crm/draft.html", {"draft": draft, "lead": draft.lead, "error": error, "newer": newer,
                                              "handoff_label": sdr.HANDOFF_REASONS.get(draft.handoff_reason, draft.handoff_reason)})

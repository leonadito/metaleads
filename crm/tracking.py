"""Public links of the CRM's e-mails: clicks (/t/<token>/), opens (open pixel,
/o/<uuid>.gif) and unsubscribe (/descadastro/<uuid>/). Opens are only a
hint (Apple Mail Privacy Protection loads every image)."""
import base64
import uuid

from django.db.models import F
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from .models import EventType, LeadEvent, OutboundMessage, Suppression, TrackedLink

PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


def _message(message_uuid):
    try:
        return OutboundMessage.objects.select_related("lead__contact", "tenant").get(uuid=uuid.UUID(str(message_uuid)))
    except (OutboundMessage.DoesNotExist, ValueError):
        raise Http404


def click(request, token):
    link = TrackedLink.objects.select_related("message__lead", "message__tenant").filter(token=token).first()
    if link is None or not link.url.startswith(("http://", "https://")):
        raise Http404
    TrackedLink.objects.filter(pk=link.pk).update(clicks=F("clicks") + 1)
    message = link.message
    LeadEvent.objects.create(lead=message.lead, tenant=message.tenant, type=EventType.EMAIL_CLICKED, actor="contato",
                             data={"outbound": str(message.uuid), "url": link.url[:500]})
    return HttpResponseRedirect(link.url)


def open_pixel(request, message_uuid):
    try:
        message = _message(message_uuid)
    except Http404:
        return HttpResponse(PIXEL, content_type="image/gif")
    if not LeadEvent.objects.filter(lead=message.lead, type=EventType.EMAIL_OPENED, data__outbound=str(message.uuid)).exists():
        LeadEvent.objects.create(lead=message.lead, tenant=message.tenant, type=EventType.EMAIL_OPENED, actor="contato",
                                 data={"outbound": str(message.uuid)})
    response = HttpResponse(PIXEL, content_type="image/gif")
    response["Cache-Control"] = "no-store"
    return response


@csrf_exempt  # the link comes from an e-mail; the form posts back to the same unguessable URL
def unsubscribe(request, message_uuid):
    message = _message(message_uuid)
    contact = message.lead.contact
    done = False
    if request.method == "POST" and contact.email:
        Suppression.objects.get_or_create(tenant=message.tenant, address=contact.email.lower(),
                                          defaults={"reason": Suppression.UNSUBSCRIBE})
        if contact.consent_email is not False:
            contact.consent_email = False
            contact.save(update_fields=["consent_email", "updated_at"])
        LeadEvent.objects.create(lead=message.lead, tenant=message.tenant, type=EventType.UNSUBSCRIBED, actor="contato",
                                 data={"outbound": str(message.uuid)})
        done = True
    return render(request, "crm/unsubscribe.html", {"company": message.tenant.name, "done": done,
                                                     "email": contact.email})

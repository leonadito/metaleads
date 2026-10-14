from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from crm.models import ApiToken, Tenant


class Command(BaseCommand):
    help = (
        "Cria um token da API de entrada de leads para um tenant. O valor vai para o arquivo "
        "indicado (chmod 600), nunca para a tela."
    )

    def add_arguments(self, parser):
        parser.add_argument("tenant_slug")
        parser.add_argument("name", help="quem usa o token, ex.: agipages")
        parser.add_argument("--output", required=True, help="arquivo que recebe METALEADS_API_TOKEN=<valor>")

    def handle(self, *args, tenant_slug, name, output, **options):
        tenant = Tenant.objects.filter(slug=tenant_slug).first()
        if tenant is None:
            raise CommandError(f"tenant {tenant_slug} não existe aqui (rode sync_tenants)")
        token, raw = ApiToken.issue(tenant, name)
        path = Path(output)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        path.write_text(f"METALEADS_API_TOKEN={raw}\n")
        self.stdout.write(f"token {token.prefix}… criado para {tenant} (gravado em {output})")

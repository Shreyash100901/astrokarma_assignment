from django.core.management.base import BaseCommand

from consultations import services


class Command(BaseCommand):
    help = (
        "Bill every in-progress session up to now and auto-end any whose wallet cannot pay "
        "the next minute. Run it periodically (cron, systemd timer, `watch`)."
    )

    def handle(self, *args, **options):
        summary = services.bill_all_active()
        self.stdout.write(
            f"checked={summary['checked']} minutes_billed={summary['minutes_billed']} "
            f"auto_ended={summary['auto_ended']}"
        )

import logging
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from apps.logistics.models import Refund, RefundStatus, PaymentStatus
from apps.logistics.payment_services import issue_refund

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Retries failed or stuck created refunds older than N minutes (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--minutes',
            type=int,
            default=15,
            help="Age threshold in minutes for pending or failed refunds to retry (default: 15)."
        )

    def handle(self, *args, **options):
        minutes = options.get('minutes', 15)
        cutoff = timezone.now() - timedelta(minutes=minutes)

        self.stdout.write(f"Searching for pending/failed refunds created on or before {cutoff} ({minutes}m cutoff)...")

        pending_refunds = Refund.objects.filter(
            status__in=[RefundStatus.CREATED, RefundStatus.FAILED],
            created_at__lte=cutoff
        ).exclude(
            payment__status=PaymentStatus.REFUNDED
        ).exclude(
            payment__refunds__status=RefundStatus.PROCESSED
        ).order_by('created_at')

        total = pending_refunds.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS("No pending or failed refunds found."))
            return

        self.stdout.write(f"Found {total} refund(s) to retry.")
        retried_count = 0
        success_count = 0
        failed_count = 0

        for refund in pending_refunds:
            payment = refund.payment
            if payment.status == PaymentStatus.REFUNDED or Refund.objects.filter(payment=payment, status=RefundStatus.PROCESSED).exists():
                continue

            retried_count += 1
            self.stdout.write(f"Retrying refund #{refund.id} (Payment #{payment.id}, status={refund.status})...")
            try:
                # If refund was stuck in CREATED, mark it FAILED so issue_refund can cleanly retry
                if refund.status == RefundStatus.CREATED:
                    refund.status = RefundStatus.FAILED
                    refund.failure_reason = "Retried by retry_pending_refunds management command"
                    refund.save(update_fields=['status', 'failure_reason', 'updated_at'])

                result = issue_refund(
                    payment=payment,
                    reason=refund.reason or "Retried by retry_pending_refunds",
                    initiated_by=refund.initiated_by,
                    source=refund.source
                )
                if result.status == RefundStatus.PROCESSED:
                    success_count += 1
                    self.stdout.write(self.style.SUCCESS(f"Refund #{result.id} processed successfully (rzp_id={result.razorpay_refund_id})."))
                else:
                    self.stdout.write(f"Refund #{result.id} status is {result.status}.")
            except Exception as e:
                failed_count += 1
                logger.error(f"Failed retrying refund #{refund.id}: {e}", exc_info=True)
                self.stdout.write(self.style.ERROR(f"Refund retry failed for Payment #{payment.id}: {e}"))

        self.stdout.write(
            self.style.SUCCESS(
                f"Completed refund retries: {retried_count} retried, {success_count} succeeded, {failed_count} failed."
            )
        )

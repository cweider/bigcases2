from datetime import timedelta
from http import HTTPStatus

from django.conf import settings
from django.core.cache import cache
from django_rq.queues import get_queue
from rest_framework.decorators import api_view, permission_classes
from rest_framework.request import Request
from rest_framework.response import Response
from rq import Retry

from bc.subscription.exceptions import (
    IdempotencyKeyMissing,
    WebhookNotSupported,
)

from .api_permissions import AllowListPermission
from .models import FilingWebhookEvent, Subscription
from .tasks import (
    check_webhook_before_posting,
    enqueue_posts_for_docket_alert,
    enqueue_posts_for_new_case,
    process_fetch_webhook_event,
    process_filing_webhook_event,
)

queue = get_queue("default")


@api_view(["POST"])
@permission_classes([AllowListPermission])
def handle_docket_alert_webhook(request: Request) -> Response:
    """
    Receives a docket alert webhook from CourtListener.
    """

    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key:
        raise IdempotencyKeyMissing()

    data = request.data
    if data["webhook"]["event_type"] != 1:
        raise WebhookNotSupported()

    cache_idempotency_key = cache.get(idempotency_key)
    if cache_idempotency_key:
        return Response(status=HTTPStatus.OK)

    sorted_results = sorted(
        data["payload"]["results"], key=lambda d: d["recap_sequence_number"]
    )
    for result in sorted_results:
        cl_docket_id = result["docket"]
        long_description = result["description"]
        document_number = result.get("entry_number")
        for doc in result["recap_documents"]:
            filing = FilingWebhookEvent.objects.create(
                docket_id=cl_docket_id,
                pacer_doc_id=doc["pacer_doc_id"],
                doc_id=doc["id"],
                document_number=document_number,
                attachment_number=doc.get("attachment_number"),
                short_description=doc["description"],
                long_description=long_description,
            )

            if doc["filepath_local"]:
                webhook_event_handler = queue.enqueue(
                    process_filing_webhook_event,
                    filing.pk,
                )
            else:
                webhook_event_handler = queue.enqueue_in(
                    timedelta(seconds=settings.WEBHOOK_DELAY_TIME),
                    process_filing_webhook_event,
                    filing.pk,
                )

            queue.enqueue(
                check_webhook_before_posting,
                filing.pk,
                depends_on=webhook_event_handler,
                retry=Retry(
                    max=settings.RQ_MAX_NUMBER_OF_RETRIES,
                    interval=settings.RQ_RETRY_INTERVAL,
                ),
            )

    # Save the idempotency key for two days after the webhook is handled
    cache.set(idempotency_key, True, 60 * 60 * 24 * 2)

    return Response(request.data, status=HTTPStatus.CREATED)


@api_view(["POST"])
@permission_classes([AllowListPermission])
def handle_recap_fetch_webhook(request: Request) -> Response:
    """
    Receives a recap fetch webhook from CourtListener.
    """
    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key:
        raise IdempotencyKeyMissing()

    data = request.data
    if data["webhook"]["event_type"] != 3:
        raise WebhookNotSupported()

    cache_idempotency_key = cache.get(idempotency_key)
    if cache_idempotency_key:
        return Response(status=HTTPStatus.OK)

    webhook_record: FilingWebhookEvent | Subscription
    try:
        # checks whether the document of the fetch webhook belongs to a filing webhook
        webhook_record = FilingWebhookEvent.objects.get(
            doc_id=data["payload"]["recap_document"]
        )
    except FilingWebhookEvent.DoesNotExist:
        # if we dont have a filing webhook related to the document, It must be an initial complaint
        webhook_record = Subscription.objects.get(
            cl_docket_id=data["payload"]["docket"]
        )

    if data["payload"]["status"] != 2:
        if isinstance(webhook_record, FilingWebhookEvent):
            webhook_record.status = FilingWebhookEvent.PURCHASE_FAILED
            webhook_record.save(update_fields=["status"])

            # schedule tasks to create the new posts(tweet and toot) without thumbnails.
            enqueue_posts_for_docket_alert(webhook_record)
        else:
            enqueue_posts_for_new_case(webhook_record)
    else:
        # schedule task to retrieve the document and create the transaction before posting.
        queue.enqueue(
            process_fetch_webhook_event,
            webhook_record.pk,
            "filing_webhook"
            if isinstance(webhook_record, FilingWebhookEvent)
            else "subscription",
            retry=Retry(
                max=settings.RQ_MAX_NUMBER_OF_RETRIES,
                interval=settings.RQ_RETRY_INTERVAL,
            ),
        )

    # Save the idempotency key for two days after the webhook is handled
    cache.set(idempotency_key, True, 60 * 60 * 24 * 2)

    return Response(request.data, status=HTTPStatus.OK)

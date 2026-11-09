"""The sync API (/api/sync/): connections, conflicts to resolve and the activity log."""

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from .. import services
from ..cardtrader import CardTraderError
from ..models import Listing, SyncConflict, SyncLog, SyncService
from .common import doc, messages_response, msg
from .errors import ServiceError

TAGS = ["sync"]
LOG_SIZE = 20


@doc(TAGS, "Connections, open conflicts and the last activity")
@api_view(["GET"])
def sync(request):
    user = request.user
    services.get_cardtrader(user)
    connections = SyncService.objects.filter(user=user).annotate(
        open_conflicts=Count("conflicts", filter=Q(conflicts__resolution=SyncConflict.Resolution.OPEN))
    )
    conflicts = (
        SyncConflict.objects.owned_by(user).filter(resolution=SyncConflict.Resolution.OPEN)
        .select_related("item__card", "service")
    )
    return Response({
        "pending_changes": Listing.objects.owned_by(user).filter(status=Listing.Status.TO_PUBLISH).count(),
        "connections": [
            {
                "name": s.name, "description": s.description, "direction": s.direction, "last_sync": s.last_sync,
                "orders_synced_at": s.orders_synced_at, "token_ok": bool(s.api_token),
                "open_conflicts": s.open_conflicts,
            }
            for s in connections
        ],
        "conflicts": [
            {
                "id": c.pk, "name": c.item.card.name, "set_code": c.item.card.set_code,
                "condition": c.item.condition, "language": c.item.language, "service": c.service.name,
                "local_value": c.local_value, "remote_value": c.remote_value,
            }
            for c in conflicts
        ],
        "log": [
            {"created_at": l.created_at, "level": l.level, "level_label": l.get_level_display(), "message": l.message}
            for l in SyncLog.objects.visible_to(user)[:LOG_SIZE]
        ],
    })


@doc(TAGS, "Sync listings with CardTrader and look for conflicts")
@api_view(["POST"])
def run(request):
    try:
        conflicts = services.sync_all(request.user)
    except CardTraderError as exc:
        raise ServiceError(f"Sync failed: {exc}")
    return messages_response(
        msg("success", f"Sync complete: {conflicts} conflict{'s' if conflicts != 1 else ''} to resolve.")
    )


@doc(TAGS, "Resolve a conflict keeping the Ledger value or the remote one (keep: ledger | remote)")
@api_view(["POST"])
def resolve(request, pk):
    conflict = get_object_or_404(
        SyncConflict.objects.owned_by(request.user), pk=pk, resolution=SyncConflict.Resolution.OPEN,
    )
    keep = request.data.get("keep")
    if keep not in (SyncConflict.Resolution.LEDGER, SyncConflict.Resolution.REMOTE):
        raise ValidationError({"detail": "Choose which source to keep.", "code": "invalid"})
    services.resolve_conflict(conflict, keep)
    return messages_response(msg("success", f"Conflict on {conflict.item.card.name} resolved."))

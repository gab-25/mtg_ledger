from .models import Listing, Order


def sidebar(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    return {
        "pending_changes": Listing.objects.owned_by(request.user).filter(status=Listing.Status.TO_PUBLISH).count(),
        "orders_to_ship": Order.objects.owned_by(request.user).filter(state=Order.State.PAID).count(),
    }

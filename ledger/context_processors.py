from .models import Listing


def sidebar(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    return {
        "pending_changes": Listing.objects.filter(status=Listing.Status.TO_PUBLISH).count(),
    }

"""Paths of the React UI's pages that the API hands to the client (links and redirects)."""

COLLECTION = "/"
PROFILE_CARDTRADER = "/account/#cardtrader"


def scan_camera(uuid):
    return f"/scan/{uuid}/"

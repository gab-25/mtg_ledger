from django.urls import path

from . import views
from .api import cards as cards_api
from .api import collection as collection_api
from .api import orders as orders_api
from .api import sale as sale_api
from .api import scan as scan_api

app_name = "ledger"

urlpatterns = [
    path("", views.collection, name="collection"),
    path("cards/add/", views.item_create, name="item_create"),
    path("cards/<int:pk>/", views.item_edit, name="item_edit"),
    path("cards/<int:pk>/delete/", views.item_delete, name="item_delete"),
    path("cards/import/", views.import_csv, name="import_csv"),
    path("cards/export/", views.export_csv, name="export_csv"),
    path("scan/", views.scan_app, name="scan_start"),
    path("scan/<path:rest>", views.scan_app, name="scan_app"),
    path("api/choices/", collection_api.choices, name="api_choices"),
    path("api/collection/", collection_api.collection, name="api_collection"),
    path("api/collection/<int:pk>/", collection_api.item, name="api_item"),
    path("api/collection/import/", collection_api.import_csv, name="api_import"),
    path("api/collection/export/", collection_api.export_csv, name="api_export"),
    path("api/listings/", sale_api.listings, name="api_listings"),
    path("api/listings/publish/", sale_api.publish, name="api_publish"),
    path("api/listings/match-prices/", sale_api.match_prices, name="api_match_prices"),
    path("api/listings/<int:item_pk>/", sale_api.listing, name="api_listing"),
    path("api/cards/", cards_api.cards, name="api_cards"),
    path("api/orders/", orders_api.orders, name="api_orders"),
    path("api/orders/import/", orders_api.import_orders, name="api_orders_import"),
    path("api/orders/<int:pk>/", orders_api.order, name="api_order"),
    path("api/orders/<int:pk>/ship/", orders_api.ship, name="api_order_ship"),
    path("api/scan/setup/", scan_api.setup, name="scan_api_setup"),
    path("api/scan/sessions/", scan_api.sessions, name="scan_api_sessions"),
    path("api/scan/sessions/<uuid:uid>/", scan_api.session_detail, name="scan_api_session"),
    path("api/scan/sessions/<uuid:uid>/captures/", scan_api.captures, name="scan_api_captures"),
    path("api/scan/sessions/<uuid:uid>/search/", scan_api.search, name="scan_api_search"),
    path("api/scan/sessions/<uuid:uid>/manual/", scan_api.manual, name="scan_api_manual"),
    path("api/scan/sessions/<uuid:uid>/review/", scan_api.review, name="scan_api_review"),
    path("api/scan/sessions/<uuid:uid>/threshold/", scan_api.threshold, name="scan_api_threshold"),
    path("api/scan/sessions/<uuid:uid>/save/", scan_api.save, name="scan_api_save"),
    path("api/scan/lines/<uuid:uid>/", scan_api.line_detail, name="scan_api_line"),
    path("api/scan/lines/<uuid:uid>/undo/", scan_api.line_undo, name="scan_api_undo"),
    path("api/scan/lines/<uuid:uid>/confirm/", scan_api.line_confirm, name="scan_api_confirm"),
    path("api/scan/lines/<uuid:uid>/frame.jpg", scan_api.line_frame, name="scan_api_frame"),
    path("sale/", views.sale, name="sale"),
    path("sale/<int:item_pk>/", views.listing_edit, name="listing_edit"),
    path("sale/<int:item_pk>/delete/", views.listing_delete, name="listing_delete"),
    path("sale/publish/", views.publish, name="publish"),
    path("sale/match-prices/", views.match_prices, name="match_prices"),
    path("cards/", views.cards, name="cards"),
    path("shipments/", views.shipments, name="shipments"),
    path("shipments/import/", views.orders_sync, name="orders_sync"),
    path("shipments/<int:pk>/", views.order_detail, name="order_detail"),
    path("shipments/<int:pk>/ship/", views.order_ship, name="order_ship"),
    path("sync/", views.sync, name="sync"),
    path("sync/all/", views.sync_all, name="sync_all"),
    path("sync/conflicts/<int:pk>/resolve/", views.resolve, name="resolve"),
]

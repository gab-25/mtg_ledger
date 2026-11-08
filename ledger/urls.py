from django.urls import path

from . import views

app_name = "ledger"

urlpatterns = [
    path("", views.collection, name="collection"),
    path("cards/add/", views.item_create, name="item_create"),
    path("cards/<int:pk>/", views.item_edit, name="item_edit"),
    path("cards/<int:pk>/delete/", views.item_delete, name="item_delete"),
    path("cards/import/", views.import_csv, name="import_csv"),
    path("cards/export/", views.export_csv, name="export_csv"),
    path("sale/", views.sale, name="sale"),
    path("sale/<int:item_pk>/", views.listing_edit, name="listing_edit"),
    path("sale/<int:item_pk>/delete/", views.listing_delete, name="listing_delete"),
    path("sale/publish/", views.publish, name="publish"),
    path("sale/match-prices/", views.match_prices, name="match_prices"),
    path("shipments/", views.shipments, name="shipments"),
    path("shipments/import/", views.orders_sync, name="orders_sync"),
    path("shipments/<int:pk>/", views.order_detail, name="order_detail"),
    path("shipments/<int:pk>/ship/", views.order_ship, name="order_ship"),
    path("sync/", views.sync, name="sync"),
    path("sync/all/", views.sync_all, name="sync_all"),
    path("sync/conflicts/<int:pk>/resolve/", views.resolve, name="resolve"),
]

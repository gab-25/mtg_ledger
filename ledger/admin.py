from django.contrib import admin

from .models import Blueprint, Card, Expansion, CollectionItem, Listing, Order, OrderItem, SyncConflict, SyncLog, SyncService


@admin.register(Card)
class CardAdmin(admin.ModelAdmin):
    list_display = ["name", "set_code", "collector_number", "type_line", "color", "ct_blueprint_id"]
    list_filter = ["color", "set_code"]
    search_fields = ["name", "type_line"]


@admin.register(CollectionItem)
class CollectionItemAdmin(admin.ModelAdmin):
    list_display = ["card", "condition", "language", "foil", "quantity", "price"]
    list_filter = ["condition", "language", "foil"]
    search_fields = ["card__name"]
    autocomplete_fields = ["card"]


@admin.register(Listing)
class ListingAdmin(admin.ModelAdmin):
    list_display = ["item", "quantity", "price", "market_price", "status", "ct_product_id"]
    list_filter = ["status"]


@admin.register(SyncService)
class SyncServiceAdmin(admin.ModelAdmin):
    list_display = ["name", "direction", "last_sync"]


@admin.register(SyncConflict)
class SyncConflictAdmin(admin.ModelAdmin):
    list_display = ["item", "service", "field", "local_value", "remote_value", "resolution"]
    list_filter = ["resolution", "service"]


@admin.register(SyncLog)
class SyncLogAdmin(admin.ModelAdmin):
    list_display = ["created_at", "level", "message"]
    list_filter = ["level"]


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    raw_id_fields = ["listing"]


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ["code", "state", "buyer_username", "ship_country_code", "size", "total", "paid_at", "sent_at"]
    list_filter = ["state", "via_cardtrader_zero"]
    search_fields = ["code", "buyer_username", "ship_name"]
    inlines = [OrderItemInline]
    readonly_fields = ["raw"]


@admin.register(Expansion)
class ExpansionAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "ct_id", "blueprints_synced_at"]
    search_fields = ["code", "name"]


@admin.register(Blueprint)
class BlueprintAdmin(admin.ModelAdmin):
    list_display = ["name", "expansion", "collector_number", "version", "rarity", "ct_id"]
    search_fields = ["name", "expansion__code"]
    list_select_related = ["expansion"]

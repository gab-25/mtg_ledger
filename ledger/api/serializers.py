"""Serializers of the scan API: requests (validation) and responses (also the OpenAPI schema).

Decimals travel as strings ("5.00"). Blueprints are identified by their CardTrader id (ct_id).
"""

import re
from decimal import Decimal, InvalidOperation

from django.db.models import Sum
from django.urls import reverse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from ..models import Blueprint, Condition, Expansion, Language, ScanLine, ScanSession
from ..recognition import same_name

MAX_FRAME_BYTES = 2 * 1024 * 1024
PHONE_UA = re.compile(r"Mobi|Android|iPhone|iPod", re.IGNORECASE)


def is_phone(request):
    """Camera scanning is for smartphones (autofocus rear camera); desktops and webcams are not supported."""
    return bool(PHONE_UA.search(request.headers.get("User-Agent", "")))


def copies(session):
    """Cards in the session, counting every copy."""
    return session.lines.aggregate(n=Sum("quantity"))["n"] or 0


class LanguageField(serializers.ChoiceField):
    """A card language code, case-insensitive on input ("it" -> "IT")."""

    def __init__(self, **kwargs):
        super().__init__(Language.choices, **kwargs)

    def to_internal_value(self, data):
        return super().to_internal_value(data.upper() if isinstance(data, str) else data)


# --- responses (read-only, so the schema marks every field as always present) ---


class ExpansionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Expansion
        fields = ["id", "code", "name"]
        read_only_fields = fields


class BlueprintSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source="ct_id", read_only=True, help_text="CardTrader blueprint id")
    expansion = ExpansionSerializer(read_only=True)

    class Meta:
        model = Blueprint
        fields = ["id", "name", "collector_number", "version", "rarity", "image_url", "expansion"]
        read_only_fields = fields


class CandidateSerializer(BlueprintSerializer):
    """A printing to choose from; context["read_name"] is the name the model read on the card."""

    matches_name = serializers.SerializerMethodField(
        help_text="It carries the name read on the card: only such a candidate should be pre-selected")

    class Meta(BlueprintSerializer.Meta):
        fields = [*BlueprintSerializer.Meta.fields, "matches_name"]
        read_only_fields = fields

    def get_matches_name(self, bp) -> bool:
        name = self.context.get("read_name", "")
        return bool(name) and same_name(bp, name)


class LineSerializer(serializers.ModelSerializer):
    blueprint = BlueprintSerializer(allow_null=True, read_only=True)
    listed = serializers.BooleanField(read_only=True, help_text="Will be queued for sale when the session is saved")
    frame_url = serializers.SerializerMethodField(help_text="The photo, kept while the card needs confirmation")
    candidates = serializers.SerializerMethodField(
        help_text="The cards to choose from when it needs confirmation, those matching read_name first")

    class Meta:
        model = ScanLine
        fields = ["uuid", "blueprint", "language", "foil", "condition", "quantity", "price", "for_sale", "listed",
                  "needs_confirmation", "read_name", "frame_url", "candidates"]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_frame_url(self, line):
        return reverse("ledger:scan_api_frame", args=[line.uuid]) if line.frame else None

    @extend_schema_field(CandidateSerializer(many=True))
    def get_candidates(self, line):
        if not (line.needs_confirmation and line.candidates):
            return []
        by_id = {b.ct_id: b for b in Blueprint.objects.filter(ct_id__in=line.candidates).select_related("expansion")}
        return CandidateSerializer([by_id[i] for i in line.candidates if i in by_id], many=True,
                                   context={"read_name": line.read_name}).data


class SessionSerializer(serializers.ModelSerializer):
    """A scan session, as seen by the device asking (context["request"])."""

    expansion = ExpansionSerializer(allow_null=True, read_only=True)
    count = serializers.SerializerMethodField(help_text="Cards scanned, counting every copy")
    last_line = serializers.SerializerMethodField(help_text="The card added or changed last")
    is_phone = serializers.SerializerMethodField()
    phone_url = serializers.SerializerMethodField(help_text="This session's scan page, to open on a phone")
    phone_secure = serializers.SerializerMethodField(help_text="False: the phone won't allow the camera (no HTTPS)")

    class Meta:
        model = ScanSession
        fields = ["uuid", "status", "expansion", "default_condition", "sale_threshold", "count", "last_line",
                  "is_phone", "phone_url", "phone_secure"]
        read_only_fields = fields

    def get_count(self, session) -> int:
        return copies(session)

    @extend_schema_field(LineSerializer(allow_null=True))
    def get_last_line(self, session):
        last = (session.lines.filter(needs_confirmation=False)
                .select_related("blueprint__expansion").order_by("-updated_at", "-id").first())
        if last is None:
            return None
        last.session = session
        return LineSerializer(last, context=self.context).data

    def get_is_phone(self, session) -> bool:
        return is_phone(self.context["request"])

    @extend_schema_field(OpenApiTypes.URI)
    def get_phone_url(self, session):
        return self.context["request"].build_absolute_uri(reverse("ledger:scan_app", args=[f"{session.uuid}/"]))

    def get_phone_secure(self, session) -> bool:
        return self.context["request"].is_secure()


class ChoiceSerializer(serializers.Serializer):
    code = serializers.CharField()
    label = serializers.CharField()


class SetupDefaultsSerializer(serializers.Serializer):
    expansion_id = serializers.IntegerField(allow_null=True)
    condition = serializers.CharField()


class MissingServiceSerializer(serializers.Serializer):
    message = serializers.CharField()
    url = serializers.CharField(help_text="Where to set it up (the profile page)")


class SetupSerializer(serializers.Serializer):
    expansions = ExpansionSerializer(many=True)
    conditions = ChoiceSerializer(many=True)
    languages = ChoiceSerializer(many=True)
    defaults = SetupDefaultsSerializer(help_text="The choices of the last session")
    missing = MissingServiceSerializer(many=True, help_text="Connections to set up before scanning")
    warnings = serializers.ListField(child=serializers.CharField())


class SessionDetailSerializer(serializers.Serializer):
    session = SessionSerializer()


class SessionCreatedSerializer(SessionDetailSerializer):
    warnings = serializers.ListField(child=serializers.CharField())


class LineResultSerializer(serializers.Serializer):
    line = LineSerializer(allow_null=True, help_text="Null when the line was removed")
    count = serializers.IntegerField(help_text="Cards in the session, counting every copy")


class CaptureResultSerializer(LineResultSerializer):
    result = serializers.ChoiceField(
        ["added", "confirm", "no_card", "handled", "busy"],
        help_text="added; confirm: pick the card among the candidates; no_card: nothing recognised; "
                  "handled: this capture_id was already processed; busy: still recognising it (HTTP 202)",
    )
    message = serializers.CharField()


class SearchResultSerializer(serializers.Serializer):
    results = BlueprintSerializer(many=True)
    where = serializers.CharField(allow_null=True, help_text="The expansion code searched, null for all")
    warnings = serializers.ListField(child=serializers.CharField())


class ReviewSerializer(serializers.Serializer):
    session = SessionSerializer()
    lines = LineSerializer(many=True)
    pending = LineSerializer(many=True, help_text="Lines that still need confirmation")
    copies = serializers.IntegerField()
    unique = serializers.IntegerField()
    listed = serializers.IntegerField(help_text="Copies that will be queued for sale")


class SaveResultSerializer(serializers.Serializer):
    saved = serializers.IntegerField()
    listed = serializers.IntegerField()
    redirect = serializers.CharField(help_text="Where to go next (the collection)")


# --- requests ---


class SessionCreateSerializer(serializers.Serializer):
    expansion_id = serializers.PrimaryKeyRelatedField(
        queryset=Expansion.objects.all(), allow_null=True, required=False, default=None,
        help_text="The expansion to scan; null for all expansions",
    )
    condition = serializers.ChoiceField(Condition.choices, default=Condition.NEAR_MINT,
                                        help_text="The default condition of the scanned cards")


class CaptureSerializer(serializers.Serializer):
    frame = serializers.FileField(help_text=f"The photo (JPEG), at most {MAX_FRAME_BYTES // (1024 * 1024)} MB")
    capture_id = serializers.CharField(max_length=64, required=False, allow_blank=True, default="",
                                       help_text="Client id of the photo: a retry with the same id adds it once")

    def validate_frame(self, frame):
        if frame.size > MAX_FRAME_BYTES:
            raise serializers.ValidationError("The photo is too large — try again.")
        return frame


class SearchQuerySerializer(serializers.Serializer):
    q = serializers.CharField(required=False, allow_blank=True, default="", help_text="Part of the card name")
    set = serializers.CharField(required=False, allow_blank=True, default="",
                                help_text="Expansion code (loaded on demand), 'all', or empty for the session's")


class PickSerializer(serializers.Serializer):
    blueprint_id = serializers.SlugRelatedField(
        slug_field="ct_id", queryset=Blueprint.objects.select_related("expansion"), help_text="CardTrader blueprint id",
    )
    language = LanguageField()
    foil = serializers.BooleanField(default=False)


class ConfirmSerializer(PickSerializer):
    condition = serializers.ChoiceField(Condition.choices, required=False, allow_null=True, default=None,
                                        help_text="Empty: the session's default condition")


class ThresholdSerializer(serializers.Serializer):
    threshold = serializers.CharField(
        required=False, allow_blank=True, default="",
        help_text="Lines priced at least this much are queued for sale, e.g. 2.50 (or 2,50); empty: none",
    )

    def validate_threshold(self, raw):
        raw = raw.strip().replace(",", ".")
        if not raw:
            return None
        try:
            value = Decimal(raw).quantize(Decimal("0.01"))
        except InvalidOperation:
            raise serializers.ValidationError("Enter the threshold as a price, e.g. 2.50.")
        return value if value.is_finite() and value >= 0 else None


class LineUpdateSerializer(serializers.Serializer):
    """The fields to change; at least one, no others."""

    foil = serializers.BooleanField(required=False)
    condition = serializers.ChoiceField(Condition.choices, required=False)
    language = LanguageField(required=False)
    quantity = serializers.IntegerField(min_value=0, required=False, help_text="0 removes the line")
    for_sale = serializers.BooleanField(required=False, allow_null=True,
                                        help_text="null: follow the session threshold")

    def validate(self, attrs):
        unknown = sorted(set(self.initial_data) - set(self.fields))
        if unknown:
            raise serializers.ValidationError({name: ["Unknown field."] for name in unknown})
        if not attrs:
            raise serializers.ValidationError("Nothing to change.")
        return attrs
